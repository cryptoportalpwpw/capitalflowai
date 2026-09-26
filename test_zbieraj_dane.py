#!/usr/bin/env python3
"""Testy zbieracza (tylko biblioteka standardowa, bez sieci).

Sprawdzają zasady projektu: brak nie jest zerem, żadnego cichego zastępowania danych,
zakres dat gdy monety publikują w różnych dniach, pamięć podręczna tylko dla młodych plików.
Uruchomienie: python3 -m unittest -v test_zbieraj_dane.py
"""
import datetime
import json
import os
import statistics
import unittest
from unittest import mock

import zbieraj_dane as zd


def _iso(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.replace(microsecond=0).isoformat()


class Fresh(unittest.TestCase):
    def test_young_file_is_fresh(self):
        self.assertTrue(zd.fresh({'at': _iso(10)}, 55))

    def test_old_file_is_not_fresh(self):
        self.assertFalse(zd.fresh({'at': _iso(120)}, 55))

    def test_broken_or_missing_at_is_not_fresh(self):
        self.assertFalse(zd.fresh({}, 55))
        self.assertFalse(zd.fresh({'at': 'wczoraj'}, 55))
        self.assertFalse(zd.fresh(None, 55))


def _soso_factory(history, funds, snapshots):
    """Udaje SoSoValue: history[sym] = wiersze summary-history, funds[sym] = lista funduszy,
    snapshots[ticker] = market-snapshot."""
    def soso(path, key, _retry=True):
        if path.startswith('/etfs/summary-history'):
            sym = path.split('symbol=')[1].split('&')[0].lower()
            return history.get(sym, [])
        if path.startswith('/etfs?'):
            sym = path.split('symbol=')[1].split('&')[0].lower()
            return funds.get(sym, [])
        if path.startswith('/etfs/') and path.endswith('/market-snapshot'):
            ticker = path.split('/')[2]
            return snapshots[ticker]
        raise AssertionError('nieznana ścieżka ' + path)
    return soso


def _rows(dates, inflow=100.0, assets=None):
    return [{'date': d, 'total_net_inflow': inflow * 1e6, 'cum_net_inflow': 5e9,
             'total_net_assets': assets} for d in dates]


class BuildEtf(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear()
        zd.META['ok'].clear()
        self.no_cg = mock.patch.object(zd, 'get_json', side_effect=RuntimeError('brak sieci'))
        self.no_cg.start()

    def tearDown(self):
        self.no_cg.stop()

    def test_missing_fund_assets_make_total_unknown_not_zero(self):
        history = {s: _rows(['2026-09-22', '2026-09-23'], assets=None) for s in zd.ETF_SYMS}
        funds = {s: [{'ticker': s.upper() + 'A', 'name': 'A'}, {'ticker': s.upper() + 'B', 'name': 'B'}] for s in zd.ETF_SYMS}
        snapshots = {}
        for s in zd.ETF_SYMS:
            snapshots[s.upper() + 'A'] = {'net_assets': 1.5e9, 'cum_inflow': 1e9, 'net_inflow': 1e6, 'sponsor_fee': 0.0025}
            snapshots[s.upper() + 'B'] = {'net_assets': None, 'cum_inflow': None, 'net_inflow': None, 'sponsor_fee': None}
        with mock.patch.object(zd, 'soso', _soso_factory(history, funds, snapshots)):
            out = zd.build_etf('klucz', '')
        for s in zd.ETF_SYMS:
            self.assertIsNone(out['assets'][s]['aum'], s)      # brak aktywów jednego funduszu = suma nieznana
            self.assertIsNone(out['assets'][s]['share'], s)

    def test_complete_fund_assets_are_summed(self):
        history = {s: _rows(['2026-09-23'], assets=None) for s in zd.ETF_SYMS}
        funds = {s: [{'ticker': s.upper() + 'A', 'name': 'A'}, {'ticker': s.upper() + 'B', 'name': 'B'}] for s in zd.ETF_SYMS}
        snapshots = {}
        for s in zd.ETF_SYMS:
            snapshots[s.upper() + 'A'] = {'net_assets': 1.5e9}
            snapshots[s.upper() + 'B'] = {'net_assets': 0.5e9}
        with mock.patch.object(zd, 'soso', _soso_factory(history, funds, snapshots)):
            out = zd.build_etf('klucz', '')
        self.assertAlmostEqual(out['assets']['btc']['aum'], 2000.0)   # mln USD

    def test_asof_is_a_range_when_coins_differ(self):
        history = {'btc': _rows(['2026-09-22', '2026-09-23']), 'eth': _rows(['2026-09-23']),
                   'sol': _rows(['2026-09-22']), 'xrp': _rows(['2026-09-22'])}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {}, {})):
            out = zd.build_etf('klucz', '')
        self.assertEqual(out['asof'], '2026-09-22 – 2026-09-23')
        self.assertEqual(out['assets']['sol']['asof'], '2026-09-22')

    def test_asof_is_single_date_when_coins_agree(self):
        history = {s: _rows(['2026-09-23']) for s in zd.ETF_SYMS}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {}, {})):
            out = zd.build_etf('klucz', '')
        self.assertEqual(out['asof'], '2026-09-23')

    def test_no_rows_for_a_coin_is_an_error_not_a_zero(self):
        # v49: moneta bez danych jest pomijana (brak klucza, błąd zapisany), pozostałe zostają; wszystkie puste = błąd
        history = {'btc': _rows(['2026-09-23']), 'eth': [], 'sol': _rows(['2026-09-23']), 'xrp': _rows(['2026-09-23'])}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {}, {})):
            out = zd.build_etf('klucz', '')
        self.assertNotIn('eth', out['assets']); self.assertEqual(sorted(out['assets']), ['btc', 'sol', 'xrp'])
        self.assertTrue(any(e.startswith('SoSoValue ETH') for e in zd.META['errors']))
        empty = {'btc': [], 'eth': [], 'sol': [], 'xrp': []}
        with mock.patch.object(zd, 'soso', _soso_factory(empty, {}, {})):
            with self.assertRaises(RuntimeError):
                zd.build_etf('klucz', '')

    def test_rows_without_inflow_are_dropped_not_zeroed(self):
        rows = _rows(['2026-09-22', '2026-09-23'])
        rows[1]['total_net_inflow'] = None
        history = {s: list(rows) for s in zd.ETF_SYMS}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {}, {})):
            out = zd.build_etf('klucz', '')
        self.assertEqual(len(out['assets']['btc']['day']), 1)
        self.assertEqual(out['asof'], '2026-09-22')

    def test_coingecko_failure_is_recorded_not_hidden(self):
        history = {s: _rows(['2026-09-23']) for s in zd.ETF_SYMS}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {}, {})):
            out = zd.build_etf('klucz', 'klucz-cg')
        self.assertEqual(out['mcap'], {})
        self.assertIs(zd.META['ok']['coingecko'], False)
        self.assertTrue(any(e.startswith('CoinGecko') for e in zd.META['errors']))


class MainFlow(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear()
        zd.META['ok'].clear()
        self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj))
        self.p_save.start()
        # testy bez sieci: źródła urzędowe udają awarię (ich własne testy są w klasie Instytucje)
        self.p_inst = mock.patch.object(zd, 'build_instytucje', side_effect=RuntimeError('offline'))
        self.p_inst.start()
        self.p_kr = mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('offline')); self.p_kr.start()
        self.p_tic = mock.patch.object(zd, 'build_tic', side_effect=RuntimeError('offline')); self.p_tic.start()
        self.p_v50 = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_v50]   # v50: nowe źródła w testach przepływu głównego bez sieci

    def tearDown(self):
        self.p_save.stop()
        self.p_inst.stop()
        self.p_kr.stop(); self.p_tic.stop()
        [p.stop() for p in self.p_v50]

    def test_young_previous_file_is_reused_without_asking_sosovalue(self):
        prev = {'at': _iso(10), 'assets': {'btc': {'day': [[1, 1.0]]}}}
        env = {'SOSOVALUE_KEY': 'k', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zd, 'previous', lambda name: prev if name == 'etf' else None), \
             mock.patch.object(zd, 'build_etf', side_effect=AssertionError('nie wolno pytać SoSoValue')):
            zd.main()
        self.assertIs(self.saved['etf'], prev)
        self.assertEqual(zd.META['ok']['sosovalue'], 'cached')

    def test_source_failure_keeps_previous_file_and_reports_error(self):
        prev = {'at': _iso(180), 'assets': {'btc': {'day': [[1, 1.0]]}}}
        env = {'SOSOVALUE_KEY': 'k', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zd, 'previous', lambda name: prev if name == 'etf' else None), \
             mock.patch.object(zd, 'build_etf', side_effect=RuntimeError('SoSoValue: 429')):
            zd.main()
        self.assertIs(self.saved['etf'], prev)                     # stary plik z prawdziwym „at”, nie pustka
        self.assertIs(zd.META['ok']['sosovalue'], False)
        self.assertTrue(any('SoSoValue' in e for e in zd.META['errors']))
        self.assertIs(zd.META['ok']['finnhub'], False)              # brak klucza = jawny błąd, nie cisza

    def test_meta_is_always_written(self):
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False):
            zd.main()
        self.assertIn('meta', self.saved)
        self.assertEqual([e for e in self.saved['meta']['errors'] if not e.startswith(('instytucje', 'Stres', 'poprzedni', 'krypto', 'TIC', 'BIS', 'CFTC', 'Coin Metrics', 'MFW', 'EBC kursy', 'obce', 'NSDL', 'TWSE', 'SAFE', 'Eurostat', 'Statistics Canada', 'FSS', 'MF SPW', 'Banxico', 'fundusze', 'BLS', 'OECD', 'Indeksy', 'Rynki', 'Wieloryby', 'Dźwignia', 'Aukcje', 'Insiderzy', 'Ceny krypto'))],
                         ['brak SOSOVALUE_KEY', 'brak FINNHUB_KEY', 'brak TWELVEDATA_KEY', 'brak COINMARKETCAP_KEY', 'brak FRED_KEY', 'brak EIA_KEY', 'brak BEA_KEY'])


class BuildDay(unittest.TestCase):
    def _quotes(self, n, dp=True):
        def get(url, headers=None, timeout=30):
            sym = url.split('symbol=')[1].split('&')[0]
            if zd.DAY_SYMS.index(sym) >= n:
                return 200, json.dumps({'c': 0, 'pc': 0})
            q = {'c': 101.0, 'pc': 100.0, 't': 1790193600}
            if dp:
                q['dp'] = 1.0
            return 200, json.dumps(q)
        return get

    def test_too_few_quotes_is_an_error(self):
        with mock.patch.object(zd, 'get', self._quotes(9)), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_day('klucz')

    def test_enough_quotes_and_missing_dp_is_computed(self):
        with mock.patch.object(zd, 'get', self._quotes(14, dp=False)), mock.patch.object(zd.time, 'sleep'):
            out = zd.build_day('klucz')
        self.assertEqual(len(out['q']), 14)
        self.assertAlmostEqual(out['q']['SPY']['dp'], 1.0)
        self.assertEqual(out['src'], 'Finnhub')

# --- Twelve Data (data/ceny.json): okresy 1T i 1M ---
def _td_symbol(dates, close0=100.0, exchange='ARCX'):
    """Odpowiedź w formacie Twelve Data: values od najnowszej do najstarszej, liczby jako teksty."""
    values = [{'datetime': d, 'open': str(close0 + i), 'high': str(close0 + i + 1), 'low': str(close0 + i - 1),
               'close': f'{close0 + i:.5f}', 'volume': str(1000 + i)} for i, d in enumerate(dates)]
    values.reverse()
    return {'meta': {'symbol': 'X', 'interval': '1day', 'currency': 'USD', 'exchange': 'NYSE', 'mic_code': exchange,
                     'type': 'ETF'}, 'values': values, 'status': 'ok'}


def _dates(n, start=datetime.date(2026, 7, 20)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _raise():
    raise AssertionError('bez klucza nie pytamy o poprzedni ceny.json')


class ParseTwelveData(unittest.TestCase):
    def test_batch_response_gives_ascending_rows_with_float_close_and_int_volume(self):
        dates = _dates(45)
        j = {'SPY': _td_symbol(dates), 'EWC': _td_symbol(dates, 50.0, 'XNMS')}
        q, errors = zd.parse_td(j, ['SPY', 'EWC'])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(q), ['EWC', 'SPY'])
        d = q['SPY']['d']
        self.assertEqual(len(d), 45)
        self.assertEqual([r[0] for r in d], dates)                       # rosnąco po dacie
        self.assertIsInstance(d[0][1], float)
        self.assertEqual(d[0][1], 100.0)
        self.assertEqual(d[-1][1], 144.0)
        self.assertEqual(d[0][2], 1000)
        self.assertEqual(q['SPY']['asof'], dates[-1])
        self.assertEqual(q['EWC']['ex'], 'XNMS')

    def test_single_symbol_response_without_outer_key_is_accepted(self):
        q, errors = zd.parse_td(_td_symbol(_dates(30)), ['SPY'])
        self.assertEqual(errors, [])
        self.assertEqual(len(q['SPY']['d']), 30)

    def test_error_symbol_goes_to_errors_not_to_quotes(self):
        j = {'SPY': _td_symbol(_dates(30)), 'TUR': {'code': 400, 'message': 'symbol not found', 'status': 'error'}}
        q, errors = zd.parse_td(j, ['SPY', 'TUR'])
        self.assertEqual(list(q), ['SPY'])
        self.assertEqual(errors, ['Twelve Data TUR: symbol not found'])

    def test_bad_key_is_a_whole_response_error(self):
        with self.assertRaises(RuntimeError):
            zd.parse_td({'code': 401, 'message': 'invalid api key', 'status': 'error'}, ['SPY', 'EWC'])

    def test_rows_without_valid_close_are_dropped_not_zeroed(self):
        o = _td_symbol(_dates(3))
        o['values'][0]['close'] = 'null'
        o['values'][1]['close'] = '0'
        q, errors = zd.parse_td({'SPY': o}, ['SPY'])
        self.assertEqual(len(q['SPY']['d']), 1)
        o['values'][2]['close'] = 'x'
        q, errors = zd.parse_td({'SPY': o}, ['SPY'])
        self.assertEqual(q, {})
        self.assertEqual(errors, ['Twelve Data SPY: brak poprawnych świec'])


class BuildPrices(unittest.TestCase):
    def _get(self, n_ok, n_candles=45, asof_shift=None):
        def get(url, headers=None, timeout=30):
            self.assertNotIn('apikey=', url.split('?')[0])
            syms = url.split('symbol=')[1].split('&')[0].split(',')
            self.assertLessEqual(len(syms), zd.TD_BATCH)
            out = {}
            for s in syms:
                if zd.DAY_SYMS.index(s) < n_ok:
                    dates = _dates(n_candles)
                    if asof_shift and s == 'EWA':
                        dates = dates[:-1]
                    out[s] = _td_symbol(dates)
                else:
                    out[s] = {'code': 404, 'message': 'no data', 'status': 'error'}
            return 200, json.dumps(out)
        return get

    def setUp(self):
        zd.META['errors'].clear()

    def test_two_batches_with_a_minute_between_them(self):
        sleeps = []
        with mock.patch.object(zd, 'get', self._get(14)), mock.patch.object(zd.time, 'sleep', sleeps.append):
            out = zd.build_prices('klucz')
        self.assertEqual(len(out['q']), 14)
        self.assertEqual(sleeps, [zd.TD_SLEEP])                            # jedna przerwa między dwiema paczkami
        self.assertEqual(out['src'], 'Twelve Data')
        self.assertEqual(out['plan'], 'basic')
        self.assertEqual(out['asof'], _dates(45)[-1])
        self.assertEqual(zd.META['errors'], [])

    def test_too_few_symbols_is_an_error(self):
        with mock.patch.object(zd, 'get', self._get(9)), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_prices('klucz')
        self.assertEqual(len([e for e in zd.META['errors'] if e.startswith('Twelve Data')]), 5)

    def test_too_few_candles_do_not_count(self):
        with mock.patch.object(zd, 'get', self._get(14, n_candles=21)), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_prices('klucz')

    def test_asof_is_a_range_when_symbols_differ(self):
        with mock.patch.object(zd, 'get', self._get(14, asof_shift=True)), mock.patch.object(zd.time, 'sleep'):
            out = zd.build_prices('klucz')
        d = _dates(45)
        self.assertEqual(out['asof'], f'{d[-2]} – {d[-1]}')

    def test_http_error_does_not_leak_the_key(self):
        import urllib.error
        def get(url, headers=None, timeout=30):
            raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None)
        with mock.patch.object(zd, 'get', get), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError) as cm:
                zd.build_prices('tajny-klucz')
        self.assertNotIn('tajny-klucz', str(cm.exception))


class MainFlowPrices(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear()
        zd.META['ok'].clear()
        self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj))
        self.p_save.start()
        self.p_inst = mock.patch.object(zd, 'build_instytucje', side_effect=RuntimeError('offline'))
        self.p_inst.start()
        self.p_kr = mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('offline')); self.p_kr.start()
        self.p_tic = mock.patch.object(zd, 'build_tic', side_effect=RuntimeError('offline')); self.p_tic.start()
        self.p_v50 = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_v50]   # v50: nowe źródła w testach przepływu głównego bez sieci

    def tearDown(self):
        self.p_save.stop()
        self.p_inst.stop()
        self.p_kr.stop(); self.p_tic.stop()
        [p.stop() for p in self.p_v50]

    def test_young_previous_file_is_reused_without_asking_twelve_data(self):
        prev = {'at': _iso(10), 'q': {'SPY': {'d': [['2026-09-23', 1.0, 1]]}}}
        env = {'SOSOVALUE_KEY': '', 'FINNHUB_KEY': '', 'COINGECKO_KEY': '', 'TWELVEDATA_KEY': 'k', 'COINMARKETCAP_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zd, 'previous', lambda name: prev if name == 'ceny' else None), \
             mock.patch.object(zd, 'build_prices', side_effect=AssertionError('nie wolno pytać Twelve Data')):
            zd.main()
        self.assertIs(self.saved['ceny'], prev)
        self.assertEqual(zd.META['ok']['twelvedata'], 'cached')

    def test_source_failure_keeps_previous_file_and_reports_error(self):
        prev = {'at': _iso(180), 'q': {'SPY': {'d': [['2026-09-23', 1.0, 1]]}}}
        env = {'SOSOVALUE_KEY': '', 'FINNHUB_KEY': '', 'COINGECKO_KEY': '', 'TWELVEDATA_KEY': 'k', 'COINMARKETCAP_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zd, 'previous', lambda name: prev if name == 'ceny' else None), \
             mock.patch.object(zd, 'build_prices', side_effect=RuntimeError('HTTP 429')):
            zd.main()
        self.assertIs(self.saved['ceny'], prev)
        self.assertIs(zd.META['ok']['twelvedata'], False)
        self.assertIn('Twelve Data: HTTP 429', zd.META['errors'])

    def test_missing_key_is_reported_and_no_file_is_written(self):
        env = {'SOSOVALUE_KEY': '', 'FINNHUB_KEY': '', 'COINGECKO_KEY': '', 'TWELVEDATA_KEY': '', 'COINMARKETCAP_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zd, 'previous', lambda name: (_raise() if name == 'ceny' else None)):
            zd.main()
        self.assertNotIn('ceny', self.saved)
        self.assertIn('brak TWELVEDATA_KEY', zd.META['errors'])
        self.assertIs(zd.META['ok']['twelvedata'], False)

class BuildCmc(unittest.TestCase):
    """CoinMarketCap global metrics: klucz w nagłówku, brakujące pola = None (nie 0), błąd statusu = wyjątek."""

    def test_metrics_parsed_from_documented_shape(self):
        seen = {}
        def get_json(url, headers=None):
            seen['url'], seen['headers'] = url, headers
            return {'status': {'error_code': 0}, 'data': {'active_cryptocurrencies': 9000, 'btc_dominance': 56.3, 'eth_dominance': 12.1,
                    'last_updated': '2026-09-24T15:00:00.000Z', 'quote': {'USD': {'total_market_cap': 2.84e12, 'total_volume_24h': 1.7e11,
                    'total_market_cap_yesterday_percentage_change': -4.9, 'stablecoin_market_cap': 3.1e11, 'defi_market_cap': 9.4e10}}}}
        with mock.patch.object(zd, 'get_json', get_json):
            out = zd.build_cmc('TAJNY-CMC')
        self.assertNotIn('TAJNY-CMC', seen['url']); self.assertEqual(seen['headers']['X-CMC_PRO_API_KEY'], 'TAJNY-CMC')
        self.assertEqual(out['total_mcap'], 2.84e12); self.assertEqual(out['btc_dom'], 56.3); self.assertEqual(out['stable_mcap'], 3.1e11)
        self.assertIsNone(out['altcoin_mcap'])                       # pola nieobecnego nie zgadujemy
        self.assertEqual(out['asof'], '2026-09-24T15:00:00.000Z')

    def test_error_status_is_an_exception_without_the_key(self):
        with mock.patch.object(zd, 'get_json', lambda url, headers=None: {'status': {'error_code': 1002, 'error_message': 'API key missing.'}}):
            with self.assertRaises(RuntimeError) as cm:
                zd.build_cmc('TAJNY-CMC')
        self.assertNotIn('TAJNY-CMC', str(cm.exception))


class Hardening(unittest.TestCase):
    """Audyt 24.09 (B6, B8, B9): klucz nigdy w meta.json, klucz CoinGecko w nagłówku, ticker sprawdzany wzorcem."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.SECRETS[:] = ['TAJNY-SOSO', 'TAJNY-CG']

    def tearDown(self):
        zd.SECRETS[:] = []

    def test_error_messages_never_contain_a_key(self):
        self.assertEqual(zd.mask('boom https://x?key=TAJNY-CG'), 'boom https://x?key=***')
        self.assertEqual(zd.mask(RuntimeError('SoSoValue TAJNY-SOSO')), 'SoSoValue ***')

    def test_coingecko_key_travels_in_a_header_not_in_the_url(self):
        seen = {}
        def get_json(url, headers=None):
            seen['url'], seen['headers'] = url, headers
            raise RuntimeError('stop')
        with mock.patch.object(zd, 'get_json', get_json), mock.patch.object(zd, 'soso', side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):
                zd.build_etf('TAJNY-SOSO', 'TAJNY-CG')
        self.assertNotIn('TAJNY-CG', seen['url'])
        self.assertEqual(seen['headers'], {'x-cg-demo-api-key': 'TAJNY-CG'})
        self.assertTrue(all('TAJNY' not in e for e in zd.META['errors']))

    def test_ticker_outside_the_pattern_is_rejected_and_reported(self):
        funds = {'btc': [{'ticker': 'IBIT', 'name': 'ok'}, {'ticker': '<img src=x>', 'name': 'zły'}]}
        snapshots = {'IBIT': {'net_assets': 1e9, 'cum_inflow': 1e9, 'net_inflow': 1e6, 'sponsor_fee': 0.0025}}
        with mock.patch.object(zd, 'soso', _soso_factory({'btc': _rows(['2026-09-22', '2026-09-23'])}, funds, snapshots)), \
             mock.patch.object(zd, 'get_json', lambda url, headers=None: {'bitcoin': {'usd_market_cap': 1e12}, 'ethereum': {'usd_market_cap': 1},
                                                                          'solana': {'usd_market_cap': 1}, 'ripple': {'usd_market_cap': 1}}), \
             mock.patch.object(zd, 'ETF_SYMS', ['btc']):
            out = zd.build_etf('k', 'c')
        self.assertEqual([f['t'] for f in out['assets']['btc']['funds']], ['IBIT'])
        self.assertTrue(any('odrzucony ticker' in e for e in zd.META['errors']))


class Instytucje(unittest.TestCase):
    """Źródła urzędowe bez klucza: parsery na kształtach odpowiedzi sprawdzonych 24.09.2026; brak nie jest zerem."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()
        self.p_sleep = mock.patch.object(zd.time, 'sleep', lambda s: None); self.p_sleep.start()   # odstępy EBC bez czekania

    def tearDown(self):
        self.p_sleep.stop()

    def test_tga_closing_balance_ascending_and_null_skipped(self):
        j = {'data': [
            {'record_date': '2026-09-22', 'account_type': 'Treasury General Account (TGA) Closing Balance', 'open_today_bal': '957409'},
            {'record_date': '2026-09-21', 'account_type': 'Treasury General Account (TGA) Closing Balance', 'open_today_bal': 'null'},
            {'record_date': '2026-09-18', 'account_type': 'Treasury General Account (TGA) Closing Balance', 'open_today_bal': '1004391'},
            {'record_date': '2026-09-18', 'account_type': 'Total TGA Deposits (Table II)', 'open_today_bal': '5'}]}
        t = zd.parse_tga(j)
        self.assertEqual(t['d'], [['2026-09-18', 1004391], ['2026-09-22', 957409]])
        self.assertEqual(t['asof'], '2026-09-22'); self.assertEqual(t['unit'], 'mln USD')
        with self.assertRaises(RuntimeError):
            zd.parse_tga({'data': []})

    def test_rrp_accepted_in_millions_only_reverse_repo(self):
        j = {'repo': {'operations': [
            {'operationDate': '2026-09-23', 'operationType': 'Reverse Repo', 'totalAmtAccepted': 461000000},
            {'operationDate': '2026-09-22', 'operationType': 'Reverse Repo', 'totalAmtAccepted': 453000000},
            {'operationDate': '2026-09-24', 'operationType': 'Repo', 'totalAmtAccepted': 1000000}]}}
        r = zd.parse_rrp(j)
        self.assertEqual(r['d'], [['2026-09-22', 453], ['2026-09-23', 461]])
        self.assertEqual(r['asof'], '2026-09-23')

    def test_soma_total_last_twelve_weeks(self):
        rows = [{'asOfDate': f'2026-0{1 + i // 4}-{1 + 7 * (i % 4):02d}', 'total': str((6000000 + i) * 1e6)} for i in range(16)]
        s = zd.parse_soma({'soma': {'summary': rows}})
        self.assertEqual(len(s['d']), 12)
        self.assertEqual(s['d'][-1][1], 6000015)

    def test_tgb_series_mapped_by_country(self):
        j = {'structure': {'dimensions': {'series': [{'id': 'FREQ', 'values': [{'id': 'M'}]}, {'id': 'REF_AREA', 'values': [{'id': 'DE'}, {'id': 'IT'}]}],
                                          'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': '2026-06'}, {'id': '2026-07'}]}]}},
             'dataSets': [{'series': {'0:0': {'observations': {'0': [1043208.02], '1': [1037116.1]}},
                                      '0:1': {'observations': {'0': [None], '1': [-331168.67]}}}}]}
        g = zd.parse_tgb(j)
        self.assertEqual(g['q']['DE'], [['2026-06', 1043208.02], ['2026-07', 1037116.1]])
        self.assertEqual(g['q']['IT'], [['2026-07', -331168.67]])       # brak obserwacji pominięty, nie zero
        self.assertEqual(g['asof'], '2026-07')

    def test_mof_period_and_columns(self):
        self.assertEqual(zd._mof_period('2026．9．6～9．12'), ('2026-09-06', '2026-09-12'))
        self.assertEqual(zd._mof_period('2025．12．28～2026．1．3'), ('2025-12-28', '2026-01-03'))
        self.assertEqual(zd._mof_period('2025．12．28～1．3'), ('2025-12-28', '2026-01-03'))
        self.assertIsNone(zd._mof_period('razem'))
        head = 'tytul,,,,,,,,,,,,,,,,,,,,,,\n"期間\nPeriod",a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v\n'
        row = '2026．9．6～9．12,"30,037 ","28,345 ","1,692 ","117,497 ","106,668 ","10,829 ","12,521 ","18,524 ","17,015 ","1,508 ","14,029 ","381,075 ","396,303 ","-15,228 ","75,958 ","53,597 ","22,362 ","7,133 ","32,871 ","45,000 ","-12,128 ","-4,995 "\n'
        m = zd.parse_mof((head + row + '(Note 1),uwaga\n').encode('cp932'))
        self.assertEqual(len(m['d']), 1)
        w = m['d'][0]
        self.assertEqual((w['from'], w['to']), ('2026-09-06', '2026-09-12'))
        self.assertEqual(w['assets']['equity_net'], 1692.0); self.assertEqual(w['assets']['total_net'], 14029.0)
        self.assertEqual(w['liabilities']['equity_net'], -15228.0); self.assertEqual(w['liabilities']['total_net'], -4995.0)
        self.assertEqual(m['unit'], '100 mln JPY')

    def test_one_failing_source_does_not_erase_the_others(self):
        def get_json(url, headers=None):
            if 'fiscaldata' in url: raise RuntimeError('timeout')
            if 'reverserepo' in url: return {'repo': {'operations': [{'operationDate': '2026-09-23', 'operationType': 'Reverse Repo', 'totalAmtAccepted': 1e6}]}}
            if 'soma' in url: return {'soma': {'summary': [{'asOfDate': '2026-09-16', 'total': '6.364e12'}]}}
            if 'ecb' in url: raise RuntimeError('503')
            raise AssertionError(url)
        with mock.patch.object(zd, 'get_json', get_json), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('cp932')):
            out = zd.build_instytucje()
        self.assertEqual(sorted(k for k in out if k not in ('at', 'src')), ['rrp', 'soma'])
        self.assertEqual(zd.META['ok'], {'tga': False, 'rrp': True, 'soma': True, 'tgb': False, 'ilm': False, 'm3': False, 'bop': False, 'mof': False})
        self.assertEqual(len(zd.META['errors']), 8)   # v49: bop ca + bop fa + bop razem

    def test_all_sources_failing_is_an_error(self):
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('down')), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                zd.build_instytucje()


class MainFlowInstytucje(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj)); self.p_save.start()
        self.p_kr = mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('offline')); self.p_kr.start()
        self.p_tic = mock.patch.object(zd, 'build_tic', side_effect=RuntimeError('offline')); self.p_tic.start()
        self.p_v50 = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_v50]   # v50: nowe źródła w testach przepływu głównego bez sieci

    def tearDown(self):
        self.p_save.stop(); self.p_kr.stop(); self.p_tic.stop()
        [p.stop() for p in self.p_v50]

    def test_young_previous_file_is_reused(self):
        prev = {'at': _iso(10), 'tga': {'d': [['2026-09-22', 1]]}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'instytucje' else None), \
             mock.patch.object(zd, 'build_instytucje', side_effect=AssertionError('bez zapytań')):
            zd.main()
        self.assertIs(self.saved['instytucje'], prev); self.assertEqual(zd.META['ok']['instytucje'], 'cached')

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': _iso(180), 'tga': {'d': [['2026-09-22', 1]]}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'instytucje' else None), \
             mock.patch.object(zd, 'build_instytucje', side_effect=RuntimeError('nic nie odpowiedziało')):
            zd.main()
        self.assertIs(self.saved['instytucje'], prev); self.assertIs(zd.META['ok']['instytucje'], False)
        self.assertIn('instytucje: nic nie odpowiedziało', zd.META['errors'])


class ParseFred(unittest.TestCase):
    """B.2 zadania „więcej danych”: FRED API, '.' = brak (nigdy zero), rosnąco po dacie, tylko serie Fed."""

    def test_dot_and_empty_values_are_skipped_and_rows_ascend(self):
        j = {'observations': [{'date': '2026-09-23', 'value': '0.461'}, {'date': '2026-09-22', 'value': '.'},
                              {'date': '2026-09-21', 'value': ''}, {'date': '2026-09-19', 'value': '2.155'}]}
        out = zd.parse_fred(j, 'RRPONTSYD')
        self.assertEqual(out['d'], [['2026-09-19', 2.155], ['2026-09-23', 0.461]])
        self.assertEqual(out['asof'], '2026-09-23'); self.assertEqual(out['unit'], 'mld USD'); self.assertEqual(out['freq'], 'D')

    def test_weekly_series_in_millions_keeps_documented_value(self):
        j = {'observations': [{'date': '2026-09-16', 'value': '6746548.0'}, {'date': '2026-09-09', 'value': '6750000.0'}]}
        out = zd.parse_fred(j, 'WALCL')
        self.assertEqual(out['d'][-1], ['2026-09-16', 6746548.0]); self.assertEqual(out['unit'], 'mln USD')

    def test_no_observations_or_error_body_is_an_error_not_a_zero(self):
        with self.assertRaises(RuntimeError):
            zd.parse_fred({'observations': [{'date': '2026-09-16', 'value': '.'}]}, 'WALCL')
        with self.assertRaises(RuntimeError) as cm:
            zd.parse_fred({'error_code': 400, 'error_message': 'Bad Request. Variable api_key is not set.'}, 'WALCL')
        self.assertIn('api_key is not set', str(cm.exception))

    def test_only_fed_series_are_configured(self):
        self.assertEqual(sorted(zd.FRED_SERIES), ['DTWEXBGS', 'RRPONTSYD', 'WALCL', 'WFASECL1', 'WMTSECL1', 'WSEFINOL', 'WSEFINTL1', 'WTREGEN'])   # v50: + depozyt H.4.1
        for third_party in ('SP500', 'VIXCLS', 'BAMLH0A0HYM2'):
            self.assertNotIn(third_party, zd.FRED_SERIES)
        self.assertIn('Board of Governors of the Federal Reserve System', zd.FRED_CITE)
        self.assertIn('not endorsed or certified by the Federal Reserve Bank of St. Louis', zd.FRED_API_NOTE)


class BuildFred(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.SECRETS[:] = ['TAJNY-FRED']
        self.p_sleep = mock.patch.object(zd.time, 'sleep', lambda s: None); self.p_sleep.start()

    def tearDown(self):
        self.p_sleep.stop(); zd.SECRETS[:] = []

    def test_one_failing_series_does_not_erase_the_others_and_key_never_leaks(self):
        def get_json(url, headers=None):
            self.assertIn('api_key=TAJNY-FRED', url); self.assertIn('file_type=json', url)
            if 'series_id=DTWEXBGS' in url:
                raise RuntimeError('HTTP 500 for ' + url)
            return {'observations': [{'date': '2026-09-16', 'value': '5'}]}
        with mock.patch.object(zd, 'get_json', get_json):
            out = zd.build_fred('TAJNY-FRED')
        self.assertEqual(sorted(out['series']), ['RRPONTSYD', 'WALCL', 'WFASECL1', 'WMTSECL1', 'WSEFINOL', 'WSEFINTL1', 'WTREGEN'])   # v50: + depozyt H.4.1
        self.assertEqual(out['custody']['total'], 5.0); self.assertIsNone(out['custody']['d1w'])   # jedna środa: zmiany = brak, nie zero
        self.assertEqual(out['src'], zd.FRED_CITE); self.assertEqual(out['api_note'], zd.FRED_API_NOTE)
        self.assertTrue(any(e.startswith('FRED DTWEXBGS:') for e in zd.META['errors']))
        self.assertTrue(all('TAJNY' not in e for e in zd.META['errors']), zd.META['errors'])

    def test_all_series_failing_is_an_error(self):
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                zd.build_fred('TAJNY-FRED')


class MainFlowFred(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj)); self.p_save.start()
        self.p_inst = mock.patch.object(zd, 'build_instytucje', side_effect=RuntimeError('offline')); self.p_inst.start()
        self.p_kr = mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('offline')); self.p_kr.start()
        self.p_tic = mock.patch.object(zd, 'build_tic', side_effect=RuntimeError('offline')); self.p_tic.start()
        self.p_v50 = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_v50]   # v50: nowe źródła w testach przepływu głównego bez sieci

    def tearDown(self):
        self.p_save.stop(); self.p_inst.stop(); self.p_kr.stop(); self.p_tic.stop()
        [p.stop() for p in self.p_v50]

    def test_young_previous_file_is_reused_without_asking_fred(self):
        prev = {'at': _iso(10), 'series': {'WALCL': {'d': [['2026-09-16', 1.0]]}}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': '', 'FRED_KEY': 'TAJNY-FRED'}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'fred' else None), \
             mock.patch.object(zd, 'build_fred', side_effect=AssertionError('bez zapytań')):
            zd.main()
        self.assertIs(self.saved['fred'], prev); self.assertEqual(zd.META['ok']['fred'], 'cached')

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': _iso(180), 'series': {'WALCL': {'d': [['2026-09-16', 1.0]]}}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': '', 'FRED_KEY': 'TAJNY-FRED'}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'fred' else None), \
             mock.patch.object(zd, 'build_fred', side_effect=RuntimeError('żadna seria FRED nie odpowiedziała')):
            zd.main()
        self.assertIs(self.saved['fred'], prev); self.assertIs(zd.META['ok']['fred'], False)
        self.assertIn('FRED: żadna seria FRED nie odpowiedziała', zd.META['errors'])

    def test_missing_key_is_reported_and_no_file_is_written(self):
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': '', 'FRED_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: None), \
             mock.patch.object(zd, 'build_fred', side_effect=AssertionError('bez zapytań')):
            zd.main()
        self.assertNotIn('fred', self.saved); self.assertIs(zd.META['ok']['fred'], False); self.assertIn('brak FRED_KEY', zd.META['errors'])


class Eurosystem(unittest.TestCase):
    """B.3 zadania „więcej danych”: bilans Eurosystemu (ILM, tygodniowo) i M3 (BSI, miesięcznie) z ECB Data Portal."""

    @staticmethod
    def _sdmx(periods, values, n_series=1):
        series = {}
        for i in range(n_series):
            series[':'.join(['0'] * 6) if n_series == 1 else f'{i}:0:0:0:0:0'] = {'observations': {str(k): [v] for k, v in enumerate(values)}}
        return {'structure': {'dimensions': {'series': [{'id': 'FREQ', 'values': [{'id': 'W'}]}],
                                             'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': p} for p in periods]}]}},
                'dataSets': [{'series': series}]}

    def test_ilm_weeks_map_to_friday_and_missing_is_skipped(self):
        j = self._sdmx(['2026-W36', '2026-W37', '2026-W38'], [5901000.4, None, 5898477])
        out = zd.parse_ilm(j)
        self.assertEqual(out['d'], [['2026-09-04', 5901000, '2026-W36'], ['2026-09-18', 5898477, '2026-W38']])
        self.assertEqual(out['asof'], '2026-09-18'); self.assertEqual(out['period'], '2026-W38'); self.assertEqual(out['unit'], 'mln EUR')

    def test_m3_monthly_keeps_period_and_documented_value(self):
        j = self._sdmx(['2026-05', '2026-06', '2026-07'], [17500000, 17550000, 17613983])
        out = zd.parse_m3(j)
        self.assertEqual(out['d'][-1], ['2026-07', 17613983]); self.assertEqual(out['asof'], '2026-07')

    def test_two_series_or_no_values_is_an_error_not_a_zero(self):
        with self.assertRaises(RuntimeError):
            zd.parse_ilm(self._sdmx(['2026-W38'], [1], n_series=2))
        with self.assertRaises(RuntimeError):
            zd.parse_m3(self._sdmx(['2026-07'], [None]))

    def test_iso_week_end(self):
        self.assertEqual(zd._iso_week_end('2026-W38'), '2026-09-18')
        self.assertEqual(zd._iso_week_end('2026-01'), None)

    def test_ecb_calls_are_spaced(self):
        calls = []
        with mock.patch.object(zd, 'get_json', lambda url, headers=None: {'u': url}), \
             mock.patch.object(zd.time, 'sleep', lambda s: calls.append(round(s, 1))), \
             mock.patch.object(zd.time, 'monotonic', lambda: 100.0):
            zd._ECB_LAST = -1e9
            zd._ecb_json('https://data-api.ecb.europa.eu/a'); zd._ecb_json('https://data-api.ecb.europa.eu/b')
        self.assertEqual(calls, [zd.ECB_SLEEP])         # pierwsze bez czekania, drugie po odstępie
        self.assertGreaterEqual(zd.ECB_SLEEP, 1.0)


class Krypto(unittest.TestCase):
    """Sekcja C zadania „więcej danych” (część bez zgód): open interest i DeFi z CoinGecko, Fear & Greed z Alternative.me."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_deriv_sums_only_numeric_open_interest_and_keeps_top5(self):
        j = [{'name': 'Binance (Futures)', 'open_interest_btc': 410657.33}, {'name': 'X', 'open_interest_btc': None},
             {'name': 'Y', 'open_interest_btc': '12'}, {'name': 'Z', 'open_interest_btc': 100.004}]
        d = zd.parse_deriv(j)
        self.assertEqual(d['n'], 2); self.assertEqual(d['total_oi_btc'], 410757.33); self.assertEqual(d['top'][0], ['Binance (Futures)', 410657.33])
        with self.assertRaises(RuntimeError):
            zd.parse_deriv([{'name': 'X', 'open_interest_btc': None}])

    def test_defi_numbers_from_text_and_missing_is_none(self):
        d = zd.parse_defi({'data': {'defi_market_cap': '131076415637.0771', 'defi_to_eth_ratio': '39.9998', 'trading_volume_24h': None, 'defi_dominance': '4.4055'}})
        self.assertAlmostEqual(d['defi_market_cap'], 131076415637.0771); self.assertIsNone(d['trading_volume_24h']); self.assertIsNone(d['eth_market_cap'])
        with self.assertRaises(RuntimeError):
            zd.parse_defi({'data': {'defi_market_cap': None}})

    def test_fng_days_ascending_and_bad_values_skipped(self):
        j = {'data': [{'value': '71', 'value_classification': 'Greed', 'timestamp': '1790208000'},
                      {'value': '78', 'value_classification': 'Extreme Greed', 'timestamp': '1790035200'},
                      {'value': 'x', 'value_classification': '?', 'timestamp': '1790121600'}]}
        f = zd.parse_fng(j)
        self.assertEqual(f['d'], [['2026-09-22', 78, 'Extreme Greed'], ['2026-09-24', 71, 'Greed']])
        self.assertEqual(f['asof'], '2026-09-24'); self.assertEqual(f['kind'], 'indicator')

    def test_one_failing_part_does_not_erase_the_others_and_key_is_a_header(self):
        seen = {}
        def get_json(url, headers=None):
            seen[url] = headers
            if 'derivatives' in url: raise RuntimeError('429')
            if 'defi' in url: return {'data': {'defi_market_cap': '1'}}
            return {'data': [{'value': '50', 'value_classification': 'Neutral', 'timestamp': '1790208000'}]}
        with mock.patch.object(zd, 'get_json', get_json):
            out = zd.build_krypto('TAJNY-CG')
        self.assertEqual(sorted(k for k in out if k not in ('at', 'src', 'attribution')), ['defi', 'fng'])
        self.assertEqual(out['attribution'], 'Data by CoinGecko')
        self.assertEqual(zd.META['ok'], {'krypto.deriv': False, 'krypto.defi': True, 'krypto.fng': True, 'krypto.mk': False, 'krypto.stabh': False, 'krypto.stabc': False})
        self.assertTrue(all('TAJNY' not in u for u in seen), 'klucz nie w adresie')
        self.assertEqual(seen[zd.CG + '/global/decentralized_finance_defi'], {'x-cg-demo-api-key': 'TAJNY-CG'})
        self.assertIsNone(seen[zd.FNG_URL])

    def test_all_parts_failing_is_an_error(self):
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                zd.build_krypto('')


class MainFlowKrypto(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj)); self.p_save.start()
        self.p_inst = mock.patch.object(zd, 'build_instytucje', side_effect=RuntimeError('offline')); self.p_inst.start()
        self.p_kr = mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('offline')); self.p_kr.start()
        self.p_tic = mock.patch.object(zd, 'build_tic', side_effect=RuntimeError('offline')); self.p_tic.start()
        self.p_v50 = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_v50]   # v50: nowe źródła w testach przepływu głównego bez sieci

    def tearDown(self):
        self.p_save.stop(); self.p_inst.stop(); self.p_kr.stop(); self.p_tic.stop()
        [p.stop() for p in self.p_v50]

    def test_young_previous_file_is_reused(self):
        prev = {'at': _iso(10), 'fng': {'d': [['2026-09-24', 71, 'Greed']]}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'krypto' else None), \
             mock.patch.object(zd, 'build_krypto', side_effect=AssertionError('bez zapytań')):
            zd.main()
        self.assertIs(self.saved['krypto'], prev); self.assertEqual(zd.META['ok']['krypto'], 'cached')

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': _iso(180), 'fng': {'d': [['2026-09-24', 71, 'Greed']]}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'krypto' else None), \
             mock.patch.object(zd, 'build_krypto', side_effect=RuntimeError('żadne źródło rynku krypto nie odpowiedziało')):
            zd.main()
        self.assertIs(self.saved['krypto'], prev); self.assertIs(zd.META['ok']['krypto'], False)
        self.assertIn('krypto: żadne źródło rynku krypto nie odpowiedziało', zd.META['errors'])


class BilansPlatniczy(unittest.TestCase):
    """B.4 zadania „więcej danych”: bilans płatniczy strefy euro z ECB Data Portal (BPS), miesięcznie."""

    @staticmethod
    def _multi(keys_rows, periods):
        # dwie serie zakodowane przez indeksy wymiarów: wymiar 0 = STO-like z wartościami po kolei
        vals = [{'id': k} for k in keys_rows]
        series = {f'{i}': {'observations': {str(j): [v] for j, v in enumerate(rows)}} for i, (k, rows) in enumerate(keys_rows.items())}
        return {'structure': {'dimensions': {'series': [{'id': 'KEY', 'values': vals}],
                                             'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': p} for p in periods]}]}},
                'dataSets': [{'series': series}]}

    def test_multi_series_are_mapped_by_full_key_and_missing_is_skipped(self):
        j = self._multi({'A': [1.5, None, 3], 'B': [None, None, None]}, ['2026-05', '2026-06', '2026-07'])
        m = zd.parse_ecb_multi(j)
        self.assertEqual(m, {'A': [['2026-05', 1.5], ['2026-07', 3]]})

    def test_bop_keeps_components_and_missing_series_is_none_not_zero(self):
        periods = ['2026-06', '2026-07']
        fa = {'structure': {'dimensions': {'series': [{'id': 'K', 'values': [{'id': k} for k in zd.BOP_FA_KEYS]}],
                                           'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': p} for p in periods]}]}},
              'dataSets': [{'series': {'0': {'observations': {'0': [10220.7459], '1': [11368.5966]}},        # fa
                                       '2': {'observations': {'0': [-204704.2331], '1': [-21793.6606]}}}}]}  # pi
        ca = {'structure': {'dimensions': {'series': [{'id': 'K', 'values': [{'id': 'CA'}]}],
                                           'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': p} for p in periods]}]}},
              'dataSets': [{'series': {'0': {'observations': {'0': [30000.4], '1': [36516.5003]}}}}]}
        b = zd.parse_bop(ca, fa)
        self.assertEqual(b['s']['ca'], [['2026-06', 30000], ['2026-07', 36517]])
        self.assertEqual(b['s']['fa'], [['2026-06', 10221], ['2026-07', 11369]])
        self.assertEqual(b['s']['pi'], [['2026-06', -204704], ['2026-07', -21794]])
        for k in ('di', 'pi_eq', 'pi_debt', 'oi'):
            self.assertIsNone(b['s'][k], k)
        self.assertEqual(b['asof'], '2026-07'); self.assertEqual(b['unit'], 'mln EUR'); self.assertIn('positive = net outflow', b['sign'])

    def test_bop_without_any_series_is_an_error(self):
        empty = {'structure': {'dimensions': {'series': [{'id': 'K', 'values': []}], 'observation': [{'id': 'TIME_PERIOD', 'values': []}]}}, 'dataSets': [{'series': {}}]}
        with self.assertRaises(RuntimeError):
            zd.parse_bop(None, empty)
        self.assertEqual(sorted(zd.BOP_FA_KEYS.values()), ['di', 'fa', 'oi', 'pi', 'pi_debt', 'pi_eq'])


class Tic(unittest.TestCase):
    """Sekcja A zadania „więcej danych”: TIC SLT — tabulatory, nagłówek techniczny, brak = None, sumy po regionach z liczbą obecnych."""
    T1 = ("Table 1\nAll Countries\nnote\nMillions of dollars\nLink\n\n|||Total\nCountry\tCountry Code\tDate\tHoldings\tNet\tVal\n"
          "country\tcountry_code\tdate\tfor_lt_total_pos\tfor_lt_total_net\tfor_lt_total_valchg\tfor_lt_treas_pos\tfor_lt_treas_net\tfor_lt_treas_valchg\tfor_lt_agcy_pos\tfor_lt_agcy_net\tfor_lt_agcy_valchg\tfor_lt_corp_pos\tfor_lt_corp_net\tfor_lt_corp_valchg\tfor_lt_eqty_pos\tfor_lt_eqty_net\tfor_lt_eqty_valchg\n"
          "Japan\t42609\t2026-07\t2998094\t-6454\t-34194\t1023754\t-8846\t-12120\t269484\t-1096\t-5091\t312507\t1495\t-5349\t1392349\t1993\t-11634\n"
          "Japan\t42609\t2026-06\t3000000\t100\t0\t1000000\t50\t0\t1\t1\t1\t1\t1\t1\t1\t40\t1\n"
          "Korea, South\t42500\t2026-07\t100\t\t0\t1\t2\t3\t4\t5\t6\t7\t8\t9\t10\t11\t12\n"
          "Grand Total\t99996\t2026-07\t38820547\t40616\t-527089\t7783259\t-3560\t-101562\t1\t1\t1\t1\t1\t1\t24341164\t3705\t-304335\n"
          "Euro area:  As of January 2026, includes Austria\n"
          "for_lt_total_net: 3 + 7 - 2\n")

    def test_table_parsed_by_technical_header_blanks_are_none_footer_ignored(self):
        t = zd.parse_tic_table(self.T1.encode())
        self.assertEqual(t['Japan']['2026-07']['for_lt_total_net'], -6454.0); self.assertEqual(t['Japan']['2026-07']['for_lt_eqty_net'], 1993.0)
        self.assertIsNone(t['Korea, South']['2026-07']['for_lt_total_net'])       # puste pole = brak, nie zero
        self.assertNotIn('Euro area:  As of January 2026, includes Austria', t); self.assertNotIn('for_lt_total_net: 3 + 7 - 2', t)
        with self.assertRaises(RuntimeError):
            zd.parse_tic_table(b'no header\n')

    def test_region_sums_count_present_members_and_missing_is_none(self):
        t = zd.parse_tic_table(self.T1.encode())
        rows = zd._tic_sum(t, ['Japan', 'Korea, South', 'Taiwan'], ['2026-06', '2026-07'], 'for_lt_total_net')
        self.assertEqual(rows, [['2026-06', 100, 1], ['2026-07', -6454, 1]])     # Korea bez liczby, Tajwanu brak → tylko Japonia
        rows = zd._tic_sum(t, ['Taiwan'], ['2026-07'], 'for_lt_total_net')
        self.assertEqual(rows, [['2026-07', None, 0]])

    def test_holders_table_in_billions_with_grand_total(self):
        raw = ("Table 5\nHoldings\nBillions of dollars\nLink\n\nCountry\t2026-07\t2026-06\t2026-05\nJapan\t1103.9\t1116.7\t1143.1\n"
               "United Kingdom\t998.3\t939.9\t948.6\nAll Other\t1842.4\t1850.3\t1\nGrand Total\t9248.1\t9298.5\t9300.0\nOf Which: Foreign Official\t3773.1\t3778.1\t1\n")
        h = zd.parse_tic_holders(raw.encode())
        self.assertEqual(h['months'], ['2026-07', '2026-06', '2026-05'])
        self.assertEqual(h['rows'][0], ['Japan', [1103.9, 1116.7, 1143.1]]); self.assertEqual(h['rows'][-1], ['Grand Total', [9248.1, 9298.5, 9300.0]])
        self.assertFalse(any(r[0].startswith('Of Which') or r[0] == 'All Other' for r in h['rows']))

    def test_build_tic_regions_world_and_optional_tables(self):
        t2 = ("x\n" * 8 + "country\tcountry_code\tdate\tus_lt_total_pos\tus_lt_total_net\tus_lt_total_valchg\tus_lt_govt_bond_pos\tus_lt_govt_bond_net\tus_lt_govt_bond_valchg\tus_lt_corp_bond_pos\tus_lt_corp_bond_net\tus_lt_corp_bond_valchg\tus_lt_eqty_pos\tus_lt_eqty_net\tus_lt_eqty_valchg\n"
              "Japan\t42609\t2026-07\t1750866\t20746\t-525\t1\t2\t3\t4\t5\t6\t7\t8\t9\nGrand Total\t99996\t2026-07\t20356009\t68522\t-158325\t1\t2\t3\t4\t5\t6\t7\t8\t9\n")
        def get_bytes(url, headers=None, timeout=60):
            if 'table1' in url: return self.T1.encode()
            if 'table2' in url: return t2.encode()
            raise RuntimeError('503')
        with mock.patch.object(zd, 'get_bytes', get_bytes):
            out = zd.build_tic()
        self.assertEqual(out['asof'], '2026-07'); self.assertEqual(out['months'], ['2026-06', '2026-07']); self.assertEqual(out['unit'], 'mln USD')
        jp = out['regions']['jpn']
        self.assertEqual(jp['in'], [['2026-06', 100, 1], ['2026-07', -6454, 1]]); self.assertEqual(jp['out'], [['2026-06', None, 0], ['2026-07', 20746, 1]])
        self.assertEqual(jp['hold_in'], ['2026-07', 2998194, 2]); self.assertEqual(jp['hold_out'], ['2026-07', 1750866, 1]); self.assertEqual(jp['n'], 2)   # Japonia + Korea (Tajwan osobno)
        self.assertEqual(jp['net'], [['2026-06', None, 0], ['2026-07', -27200, 1]])   # netto tylko z krajów obecnych w obu tabelach
        self.assertIn('twn', out); self.assertEqual(out['twn']['members'], ['Taiwan'])
        self.assertEqual(out['world']['in'][-1], ['2026-07', 40616, 1]); self.assertEqual(out['world']['out'][-1], ['2026-07', 68522, 1])
        self.assertEqual(out['regions']['can']['in'][-1], ['2026-07', None, 0])           # brak Kanady w próbce → brak, nie zero
        self.assertIsNone(out['holders']); self.assertTrue(any(e.startswith('TIC tabela 5') for e in zd.META['errors']))
        self.assertIn('positive = capital into the USA', out['sign'])


class KryptoV49(unittest.TestCase):
    """v49: zmiany 30D/1R z CoinGecko (CoinPaprika free zwraca 0) i historia podaży stablecoinów na serwerze."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_markets_rows_dedupe_symbols_and_missing_is_none(self):
        p1 = [{'symbol': 'btc', 'market_cap': 1.69e12, 'price_change_percentage_24h_in_currency': -0.07, 'price_change_percentage_7d_in_currency': 10.2,
               'price_change_percentage_30d_in_currency': 6.5954, 'price_change_percentage_1y_in_currency': -25.7682, 'last_updated': '2026-09-24T20:35:20.000Z'},
              {'symbol': 'btc', 'market_cap': 1, 'price_change_percentage_30d_in_currency': 99},
              {'symbol': 'usdt', 'market_cap': 1.8e11, 'price_change_percentage_1y_in_currency': None}]
        m = zd.parse_mk([p1, []])
        self.assertEqual(m['rows'][0], ['BTC', 1.69e12, -0.07, 10.2, 6.5954, -25.7682])
        self.assertEqual(len(m['rows']), 2); self.assertIsNone(m['rows'][1][5]); self.assertEqual(m['asof'], '2026-09-24T20:35:20')
        with self.assertRaises(RuntimeError):
            zd.parse_mk([{'error': 'x'}])

    def test_stablecoin_history_changes_are_supply_not_price(self):
        day = 86400; t0 = 1790208000
        j = [{'date': str(t0 - k * day), 'totalCirculatingUSD': {'peggedUSD': 300e9 + (400 - k) * 1e8}} for k in range(400, -1, -1)]
        h = zd.parse_stabh(j)
        self.assertEqual(h['asof'], '2026-09-24'); self.assertEqual(h['cur'], round(300e9 + 400 * 1e8))
        self.assertEqual(h['d']['1'], round(1e8)); self.assertEqual(h['d']['30'], round(30e8)); self.assertEqual(h['d']['365'], round(365e8))
        self.assertAlmostEqual(h['pct']['7'], round((340e9 / (340e9 - 7e8) - 1) * 100, 4))
        with self.assertRaises(RuntimeError):
            zd.parse_stabh([{'date': '1', 'totalCirculatingUSD': {'peggedUSD': 1}}])


class RobustnessV49(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.SECRETS[:] = []   # inne testy uruchamiają main() z kluczami z otoczenia

    def test_one_finnhub_symbol_error_does_not_stop_the_rest(self):
        calls = []
        def get(url, headers=None, timeout=30):
            calls.append(url)
            if 'symbol=EWC' in url:
                raise RuntimeError('HTTP 429 for token=TAJNY')
            return 200, json.dumps({'c': 10.0, 'pc': 9.0, 'dp': 11.1, 't': 1})
        zd.SECRETS[:] = ['TAJNY']
        try:
            with mock.patch.object(zd, 'get', get), mock.patch.object(zd.time, 'sleep', lambda s: None):
                out = zd.build_day('TAJNY')
        finally:
            zd.SECRETS[:] = []
        self.assertEqual(len(calls), len(zd.DAY_SYMS)); self.assertNotIn('EWC', out['q']); self.assertEqual(len(out['q']), len(zd.DAY_SYMS) - 1)
        self.assertTrue(any(e.startswith('Finnhub EWC') and 'TAJNY' not in e for e in zd.META['errors']))

    def test_bop_current_account_failure_keeps_financial_account(self):
        fa = {'structure': {'dimensions': {'series': [{'id': 'K', 'values': [{'id': k} for k in zd.BOP_FA_KEYS]}],
                                           'observation': [{'id': 'TIME_PERIOD', 'values': [{'id': '2026-07'}]}]}},
              'dataSets': [{'series': {'0': {'observations': {'0': [11368.6]}}}}]}
        def ecb(url):
            if 'T.B.CA.' in url: raise RuntimeError('access blocked')
            return fa
        with mock.patch.object(zd, '_ecb_json', ecb):
            b = zd.parse_bop(zd._ecb_try(zd.BOP_CA_URL, 'bop ca'), zd._ecb_try(zd.BOP_FA_URL, 'bop fa'))
        self.assertIsNone(b['s']['ca']); self.assertEqual(b['s']['fa'], [['2026-07', 11369]])
        self.assertIn('bop ca: access blocked', zd.META['errors'])


class BisLbs(unittest.TestCase):
    """v50: BIS LBS miara F — prawdziwe wiersze z odpowiedzi BIS z 24.09.2026 (wycinek), brak = None, salda na parach krajów."""
    HEAD = ('FREQ,L_MEASURE,L_POSITION,L_INSTR,L_DENOM,L_CURR_TYPE,L_PARENT_CTY,L_REP_BANK_TYPE,L_REP_CTY,L_CP_SECTOR,'
            'L_CP_COUNTRY,L_POS_TYPE,DECIMALS,UNIT_MEASURE,UNIT_MULT,AVAILABILITY,TITLE_GRP,TIME_FORMAT,COLLECTION,'
            'ORG_VISIBILITY,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK')
    # raportujący,kontrahent,kwartał,wartość (mln USD),status — wycinek odpowiedzi BIS_URL z 24.09.2026
    REAL = """GB,US,2025-Q4,-3166.185,A
GB,US,2026-Q1,194782.426,A
US,GB,2025-Q4,-30041.572,A
US,GB,2026-Q1,83065.607,A
DE,US,2025-Q4,-2044.566,A
DE,US,2026-Q1,83688.666,A
US,DE,2025-Q4,12255.226,A
US,DE,2026-Q1,-4162.079,A
FR,US,2025-Q4,-22266.654,A
FR,US,2026-Q1,9684.247,A
US,FR,2025-Q4,10462.481,A
US,FR,2026-Q1,-28926.626,A
IT,US,2025-Q4,-1689.259,A
IT,US,2026-Q1,845.285,A
US,IT,2025-Q4,1444.204,A
US,IT,2026-Q1,-297.234,A
ES,US,2025-Q4,9287.087,A
ES,US,2026-Q1,6161.192,A
US,ES,2025-Q4,-573.321,A
US,ES,2026-Q1,2499.191,A
NL,US,2025-Q4,3992.794,A
NL,US,2026-Q1,3636.218,A
US,NL,2025-Q4,-1535.223,A
US,NL,2026-Q1,1082.293,A
CH,US,2025-Q4,9277.264,A
CH,US,2026-Q1,198.058,A
US,CH,2025-Q4,-2172.68,A
US,CH,2026-Q1,2632.248,A
SE,US,2025-Q4,-7265.643,A
SE,US,2026-Q1,3402.082,A
US,SE,2025-Q4,-13091.585,A
US,SE,2026-Q1,11438.216,A
JP,US,2025-Q4,69752.073,A
JP,US,2026-Q1,108345.812,A
KR,US,2025-Q4,3712.058,A
KR,US,2026-Q1,3205.537,A
HK,US,2025-Q4,30742.922,A
HK,US,2026-Q1,-18637.593,A
US,JP,2025-Q4,31172.02,A
US,JP,2026-Q1,-23517.386,A
US,KR,2025-Q4,-66.076,A
US,KR,2026-Q1,988.328,A
US,HK,2025-Q4,-6669.386,A
US,HK,2026-Q1,12479.868,A
ZA,GB,2025-Q4,1767.63,A
ZA,GB,2026-Q1,836.013,A
CA,RU,2025-Q4,-0.969,A
CA,RU,2026-Q1,0.132,A
AU,SG,2025-Q4,5986.833,A
AU,SG,2026-Q1,-483.505,A
AU,NZ,2025-Q4,389.123,A
AU,NZ,2026-Q1,1136.897,A
CA,MY,2025-Q4,NaN,Q
CA,MY,2026-Q1,NaN,Q
CA,MX,2025-Q4,NaN,Q
CL,EG,2022-Q2,-0.032,A
CL,IL,2022-Q2,-0.053,A"""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    @classmethod
    def row(cls, rep, cp, q, v, status='A', unit='USD', mult='6', measure='F'):
        return f'Q,{measure},C,A,TO1,A,5J,A,{rep},A,{cp},N,3,{unit},{mult},K,,,S,E,{q},{v},{status},F,'

    @classmethod
    def csv_of(cls, rows):
        return (cls.HEAD + '\n' + '\n'.join(rows) + '\n').encode()

    @classmethod
    def real(cls):
        return cls.csv_of([cls.row(*ln.split(',')) for ln in cls.REAL.splitlines()])

    # siedem par regionów (próg jakości to 6) w czterech kolejnych kwartałach
    Q4 = ['2025-Q2', '2025-Q3', '2025-Q4', '2026-Q1']

    def base4(self, skip=()):
        pairs = [('GB', 'US', 100), ('US', 'GB', 40), ('JP', 'US', 7), ('AU', 'SG', 2), ('ZA', 'GB', 3), ('CA', 'RU', 1), ('HK', 'IN', 5), ('BR', 'US', 4)]
        return [self.row(a, b, q, v) for a, b, v in pairs for q in self.Q4 if (a, b, q) not in skip]

    def test_real_rows_give_the_known_corridors(self):
        j = zd.parse_bis_flows(self.real(), at='2026-09-24T21:00:00+00:00')
        self.assertEqual(j['asof'], '2026-Q1'); self.assertEqual(j['unit'], 'mln USD')
        self.assertEqual(j['quarters'], ['2025-Q2', '2025-Q3', '2025-Q4', '2026-Q1'])   # okno 4 kolejnych kwartałów; stare 2022-Q2 nie liczą się
        f = j['flows']
        self.assertEqual(f['eur>usa'][-1], ['2026-Q1', 302398.2, 8])       # 8 krajów Europy → USA (tyle samo co w pełnej odpowiedzi)
        self.assertEqual(f['eur>usa'][-2], ['2025-Q4', -13875.2, 8])       # minus = banki ograniczyły należności
        self.assertEqual(f['usa>eur'][-1], ['2026-Q1', 67331.6, 8])
        self.assertEqual(f['jpn>usa'][-1], ['2026-Q1', 111551.3, 2])
        self.assertEqual(f['chn>usa'][-1], ['2026-Q1', -18637.6, 1])       # Chiny jako pożyczający = tylko Hongkong
        self.assertEqual(f['eur>usa'][0], ['2025-Q2', None, 0])            # kwartał w oknie bez danych = brak, nie zero
        self.assertIsNone(j['regions']['eur']['out4'])                     # suma 4 kwartałów tylko z kompletu
        self.assertEqual(f['can>rus'][-1], ['2026-Q1', 0.1, 1])            # 0,132 mln: mała liczba to liczba, nie brak
        self.assertNotIn('can>asean', f); self.assertNotIn('can>lat', f)   # Q (poufne) i NaN = brak, nie 0
        self.assertNotIn('oce>oce', f)                                     # AU→NZ: ten sam region
        self.assertIn('can|rus', j['oneway']); self.assertIn('eur|usa', j['pairs'])
        self.assertEqual(j['regions']['eur']['cp'], zd.BIS_CP['eur'])
        self.assertEqual(j['regions']['eur']['rep_q'][0], ['GB', 194782.4, 1])   # wkład Wielkiej Brytanii (Londyn) w wypływ Europy
        self.assertEqual(j['regions']['oce']['rep_q'], [['AU', -483.5, 1]])      # AU→NZ (ten sam region) nie jest wypływem regionu
        self.assertIn('rus', j['no_reporter']); self.assertIn('ind', j['no_reporter']); self.assertIn('mea', j['no_reporter'])
        self.assertTrue(all(x[1] is None and x[2] == 0 for x in j['regions']['rus']['out']))

    def test_net_uses_only_matched_country_pairs_and_sums_to_zero(self):
        j = zd.parse_bis_flows(self.real())
        v = {(a, b, q): float(x) for a, b, q, x, s in (ln.split(',') for ln in self.REAL.splitlines()) if s == 'A'}
        eur = sum(v[(c, 'US', '2026-Q1')] - v[('US', c, '2026-Q1')] for c in zd.BIS_CP['eur'])
        self.assertEqual(j['regions']['eur']['net'][-1], ['2026-Q1', round(eur, 1), 8])   # ZA→GB bez GB→ZA nie wchodzi do salda
        self.assertGreater(j['regions']['eur']['net'][-1][1], 0)                         # plus = Europa netto pożycza innym
        for i in range(4):
            s = sum(r['net'][i][1] for r in j['regions'].values() if r['net'][i][1] is not None)
            self.assertAlmostEqual(s, 0, delta=1.0)
        self.assertEqual(j['regions']['afr']['net'][-1], ['2026-Q1', None, 0])          # brak pary dwustronnej = brak, nie 0
        self.assertEqual(j['regions']['afr']['out'][-1], ['2026-Q1', 836.0, 1])

    def test_missing_statuses_nan_and_empty_are_missing_not_zero(self):
        rows = self.base4() + [self.row('DE', 'CN', '2026-Q1', 'NaN', 'Q'), self.row('DE', 'CN', '2025-Q4', '9', 'K'),
                               self.row('FR', 'CN', '2026-Q1', ''), self.row('IT', 'CN', '2026-Q1', '5', 'M')]
        j = zd.parse_bis_flows(self.csv_of(rows))
        self.assertNotIn('eur>chn', j['flows'])

    def test_totals_need_the_full_window(self):
        j = zd.parse_bis_flows(self.csv_of(self.base4()))
        self.assertEqual(j['regions']['eur']['out4'], 4 * 100 + 0.0)
        self.assertEqual(j['regions']['usa']['net4'], 4 * (40 - 100) + 0.0)
        j = zd.parse_bis_flows(self.csv_of(self.base4(skip={('GB', 'US', '2025-Q3')})))
        self.assertIsNone(j['regions']['eur']['out4']); self.assertEqual(j['flows']['eur>usa'][1], ['2025-Q3', None, 0])

    def test_latest_quarter_must_be_full(self):
        rows = self.base4() + [self.row('GB', 'US', '2026-Q2', '1')]      # szczątkowa świeża publikacja nie przesuwa okna
        j = zd.parse_bis_flows(self.csv_of(rows))
        self.assertEqual(j['asof'], '2026-Q1')

    def test_unit_multiplier_currency_measure_and_columns(self):
        j = zd.parse_bis_flows(self.csv_of(self.base4() + [self.row('DE', 'CN', '2026-Q1', '2', mult='9')]))
        self.assertEqual(j['flows']['eur>chn'][-1], ['2026-Q1', 2000.0, 1])   # 2 mld = 2000 mln
        with self.assertRaises(RuntimeError):
            zd.parse_bis_flows(self.csv_of(self.base4() + [self.row('DE', 'CN', '2026-Q1', '2', unit='EUR')]))
        j = zd.parse_bis_flows(self.csv_of(self.base4() + [self.row('DE', 'CN', '2026-Q1', '999999', measure='S')]))
        self.assertNotIn('eur>chn', j['flows'])                             # stan (S) zamiast zmiany (F) nie trafia do sum
        with self.assertRaises(RuntimeError):
            zd.parse_bis_flows(b'FREQ,TIME_PERIOD,OBS_VALUE\nQ,2026-Q1,1\n')
        with self.assertRaises(RuntimeError):
            zd.parse_bis_flows(self.csv_of([]))
        with self.assertRaises(RuntimeError):
            zd.parse_bis_flows(self.csv_of(self.base4()[:8]))               # za mało par regionów

    def test_url_is_measure_f_without_key_and_regions_are_consistent(self):
        self.assertIn('/Q.F.C.A.TO1.A.5J.A.', zd.BIS_URL); self.assertIn('lastNObservations=5', zd.BIS_URL)
        self.assertTrue(zd.BIS_URL.startswith('https://stats.bis.org/')); self.assertNotIn('key', zd.BIS_URL.lower())
        c2r = {c: r for r, cs in zd.BIS_CP.items() for c in cs}
        for r, cs in zd.BIS_REP.items():
            for c in cs:
                self.assertEqual(c2r[c], r)
        self.assertEqual(zd._bis_qshift('2026-Q1', 1), '2025-Q4'); self.assertEqual(zd._bis_qshift('2026-Q1', 4), '2025-Q1')

    def test_build_bis_single_request_with_file_time(self):
        calls = []
        def get_bytes(url, headers=None, timeout=60):
            calls.append((url, timeout)); return self.real()
        with mock.patch.object(zd, 'get_bytes', get_bytes):
            out = zd.build_bis()
        self.assertEqual(calls, [(zd.BIS_URL, 90)]); self.assertEqual(out['at'], zd.NOW)
        self.assertIn('positive = banks in a lent/placed more in b', out['sign'])
        self.assertLess(len(json.dumps(out)), 60000)


class MainFlowBis(unittest.TestCase):
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.p_save = mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj)); self.p_save.start()
        self.p_off = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                      for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.p_off]

    def tearDown(self):
        self.p_save.stop(); [p.stop() for p in self.p_off]

    def test_young_previous_file_is_reused(self):
        prev = {'at': _iso(60), 'asof': '2026-Q1', 'flows': {}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'bis' else None), \
             mock.patch.object(zd, 'build_bis', side_effect=AssertionError('bez zapytań')):
            zd.main()
        self.assertIs(self.saved['bis'], prev); self.assertEqual(zd.META['ok']['bis'], 'cached')

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': _iso(26 * 60), 'asof': '2026-Q1', 'flows': {}}
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'bis' else None), \
             mock.patch.object(zd, 'build_bis', side_effect=RuntimeError('HTTP Error 503')):
            zd.main()
        self.assertIs(self.saved['bis'], prev); self.assertIs(zd.META['ok']['bis'], False)
        self.assertIn('BIS: HTTP Error 503', zd.META['errors'])

    def test_fresh_build_is_saved(self):
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        new = {'at': zd.NOW, 'asof': '2026-Q1'}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: None), \
             mock.patch.object(zd, 'build_bis', return_value=new):
            zd.main()
        self.assertIs(self.saved['bis'], new); self.assertIs(zd.META['ok']['bis'], True)



# v50 CFTC: prawdziwe wiersze FinFutWk.txt (raport na 15.09.2026, pobrane 24.09.2026, końce linii CRLF jak w pliku CFTC);
# MICRO BITCOIN (133742) ma zostać pominięty. Historia w testach powstaje z tych wierszy (inna data, przesunięte pozycje).
_CFTC_WK = (
    '"EURO FX - CHICAGO MERCANTILE EXCHANGE",260915,2026-09-15,099741,CME ,00,099 ,  920035,   41113,  299193,    5144,  486435,  234737,   43899,  103260,  131416,   23388,   24818,   17063,    3579,  731636,  758419,  188399,  161616,  -22429,  -13244,  -16618,  -73160,    1992,     972,   -1665,    8452,    3323,  -37719,     231,     651,   -2934, -118047, -127150,   95618,  104721,  100.0,    4.5,   32.5,    0.6,   52.9,   25.5,    4.8,   11.2,   14.3,    2.5,    2.7,    1.9,    0.4,   79.5,   82.4,   20.5,   17.6,    318,     16,     13,      8,    116,     43,     49,     48,     48,     23,     21,     13,      6,    235,    169,    17.7,    30.6,    25.5,    45.6,    17.5,    30.6,    25.2,    44.5,"(CONTRACTS OF EUR 125,000)","099741","CME ","099 ","F10","FutOnly"\r\n'
    '"BITCOIN - CHICAGO MERCANTILE EXCHANGE",260915,2026-09-15,133741,CME ,00,133 ,   20773,    6587,    3168,     620,    4528,    1768,     486,    5545,   11899,    1841,     122,     136,      23,   19752,   19941,    1021,     832,    -310,    -212,    -688,      31,    -422,     561,     185,     399,   -1139,     585,    -647,      36,      23,     -58,    -406,    -252,      96,  100.0,   31.7,   15.3,    3.0,   21.8,    8.5,    2.3,   26.7,   57.3,    8.9,    0.6,    0.7,    0.1,   95.1,   96.0,    4.9,    4.0,    111,     12,      9,      4,      6,      7,      5,     26,     42,     16,      4,.,.,     63,     74,    61.0,    28.6,    71.7,    47.0,    59.6,    25.3,    68.2,    41.8,"(5 Bitcoins)","133741","CME ","133 ","F85","FutOnly"\r\n'
    '"MICRO BITCOIN - CHICAGO MERCANTILE EXCHANGE",260915,2026-09-15,133742,CME ,00,133 ,   37455,    3991,    6069,       0,    4700,     571,       0,   16881,   25363,     405,    6047,    2847,      87,   32111,   35342,    5344,    2113,    2276,    2804,    -116,       0,    3154,     532,     -10,   -2378,     698,     157,    -976,     766,      17,    2768,    2044,    -492,     232,  100.0,   10.7,   16.2,    0.0,   12.5,    1.5,    0.0,   45.1,   67.7,    1.1,   16.1,    7.6,    0.2,   85.7,   94.4,   14.3,    5.6,    198,      8,      4,      0,      8,.,      0,     51,     15,      6,     74,     36,.,    146,     61,    32.6,    76.1,    49.9,    82.4,    32.6,    75.7,    49.9,    81.9,"(Bitcoin X $0.10)","133742","CME ","133 ","F85","FutOnly"\r\n'
    '"ETHER CASH SETTLED - CHICAGO MERCANTILE EXCHANGE",260915,2026-09-15,146021,CME ,00,146 ,   28413,   19586,    9015,     980,    1419,    3339,     460,    3293,   11015,    1939,     159,    1282,       0,   27836,   28030,     577,     383,    1849,     -48,   -1216,     685,    -206,      61,     -39,     197,     633,    1581,       2,     180,       0,    2172,    1885,    -323,     -36,  100.0,   68.9,   31.7,    3.4,    5.0,   11.8,    1.6,   11.6,   38.8,    6.8,    0.6,    4.5,    0.0,   98.0,   98.7,    2.0,    1.3,    101,      5,     10,.,      5,      8,.,     29,     34,     14,      5,      4,      0,     55,     65,    81.5,    35.8,    86.8,    54.3,    75.2,    32.0,    78.5,    47.9,"(50 Index Points)","146021","CME ","146 ","F85","FutOnly"\r\n')
_CFTC_TODAY = datetime.date(2026, 9, 24)


def _cftc_row(line, day, delta=0, **cols):
    """Ten sam wiersz z innego tygodnia: data `day`; dealer long +delta, pozostali short +delta, open interest +delta
    (sumy dalej równe OI, więc strażnik go przepuszcza; netto dealerów +delta, pozostałych −delta); cols = nadpisane pola."""
    import csv, io
    p = next(csv.reader([line.strip()]))
    C = zd.CFTC_COLS
    p[C.index('Report_Date_as_YYYY-MM-DD')] = day
    p[C.index('As_of_Date_In_Form_YYMMDD')] = day[2:].replace('-', '')
    for c in ('Open_Interest_All', 'Dealer_Positions_Long_All', 'Other_Rept_Positions_Short_All'):
        p[C.index(c)] = str(int(p[C.index(c)]) + delta)
    for c, v in cols.items():
        p[C.index(c)] = v
    buf = io.StringIO(); csv.writer(buf, lineterminator='\n').writerow(p)
    return buf.getvalue()


def _cftc_weeks(last, n):
    d = datetime.date.fromisoformat(last)
    return [(d - datetime.timedelta(days=7 * k)).isoformat() for k in range(n - 1, -1, -1)]


def _cftc_year(days, skip=()):
    """Plik roczny (zip z FinFutYY.txt, z nagłówkiem): każdy tydzień z `days` dla każdego wiersza próbki; tydzień o k wcześniej
    niż ostatni ma delta = 100·k. skip = kody rynków pominiętych."""
    import io, zipfile
    lines = [l for l in _CFTC_WK.split('\r\n') if l and not any(f',{c},' in l for c in skip)]
    text = ','.join(zd.CFTC_COLS) + '\n' + ''.join(_cftc_row(l, d, 100 * (len(days) - 1 - i)) for i, d in enumerate(days) for l in lines)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('FinFutYY.txt', text)
    return buf.getvalue()


def _cftc_fetch(mapping):
    """Udaje sieć: URL → bajty albo wyjątek; nieznany URL → HTTP 404 (jak nieistniejący plik roczny CFTC)."""
    import urllib.error
    def fetch(url):
        v = mapping.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        return v
    return fetch


def _cftc_std(**over):
    m = {zd.CFTC_WEEK_URL: _CFTC_WK.encode(), zd.CFTC_YEAR_URL.format(2026): _cftc_year(_cftc_weeks('2026-09-15', 14))}
    m.update(over)
    return m


class Cftc(unittest.TestCase):
    """v50: CFTC Traders in Financial Futures wprost z cftc.gov — brak to None (nigdy 0), strażnik sum, historia z okna 90 dni."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.SECRETS[:] = []   # inne testy zostawiają klucze — mask() psułby komunikaty

    def test_weekly_file_without_header_real_numbers_only_our_markets(self):
        w = zd.parse_cftc_csv(_CFTC_WK, header=zd.CFTC_COLS)
        self.assertEqual(sorted(w), ['099741', '133741', '146021'])                 # MICRO BITCOIN 133742 pominięty
        self.assertEqual(len(zd.CFTC_COLS), 87)
        r = zd.cftc_record(w['099741']['2026-09-15'])
        d = r['g']['dealer']
        self.assertEqual((d['long'], d['short'], d['spread'], d['net'], d['chg_net']), (41113, 299193, 5144, -258080, 3374))
        self.assertEqual((r['g']['asset_mgr']['net'], r['g']['lev_funds']['net'], r['g']['other_rept']['net']), (251698, -28156, 7755))
        self.assertEqual(r['g']['lev_funds']['chg_net'], 5129)
        n = r['g']['nonrept']
        self.assertEqual((n['long'], n['short'], n['net'], n['chg_net']), (188399, 161616, 26783, -9103))
        self.assertIsNone(n['spread'])                                              # CFTC nie dzieli małych graczy — brak, nie 0
        self.assertEqual((r['oi'], r['oi_chg'], r['units']), (920035, -22429, '(CONTRACTS OF EUR 125,000)'))
        self.assertTrue(zd.cftc_consistent(r))
        eth = zd.cftc_record(w['146021']['2026-09-15'])
        self.assertEqual(eth['g']['other_rept']['spread'], 0)                       # prawdziwe zero z raportu zostaje zerem
        self.assertEqual(eth['units'], '(50 Index Points)')

    def test_missing_values_bad_dates_and_other_layouts_are_gaps_not_zeros(self):
        for tok in ('.', '', 'nan', 'inf', '-', 'x'):
            self.assertIsNone(zd._cftc_int(tok), tok)
        self.assertEqual(zd._cftc_int(' -22429'), -22429)
        line = _CFTC_WK.split('\r\n')[1]
        row = zd.parse_cftc_csv(_cftc_row(line, '2026-09-15', Change_in_Dealer_Long_All='.', Change_in_Open_Interest_All='nan'),
                                header=zd.CFTC_COLS)['133741']['2026-09-15']
        r = zd.cftc_record(row)
        self.assertIsNone(r['g']['dealer']['chg_net']); self.assertIsNone(r['oi_chg']); self.assertEqual(r['g']['dealer']['net'], 3419)
        self.assertEqual(zd.parse_cftc_csv(_cftc_row(line, '2026-13-45'), header=zd.CFTC_COLS), {})   # nieistniejąca data
        self.assertEqual(zd.parse_cftc_csv(line.rsplit(',', 1)[0] + '\n', header=zd.CFTC_COLS), {})  # 86 pól zamiast 87
        with self.assertRaises(RuntimeError):
            zd.parse_cftc_csv('a,b,c\n1,2,3\n')

    def test_guard_catches_shifted_columns(self):
        row = dict(zd.parse_cftc_csv(_CFTC_WK, header=zd.CFTC_COLS)['099741']['2026-09-15'])
        row['Dealer_Positions_Long_All'] = row['Dealer_Positions_Short_All']          # kolumna przesunięta o jedną w prawo
        self.assertFalse(zd.cftc_consistent(zd.cftc_record(row)))
        self.assertFalse(zd.cftc_consistent(zd.cftc_record(dict(row, Dealer_Positions_Long_All='.'))))

    def test_build_state_history_13_reports_and_order(self):
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std()), today=_CFTC_TODAY)
        self.assertEqual(zd.META['errors'], [])
        self.assertEqual((out['asof'], out['unit'], out['url']), ('2026-09-15', 'kontrakty', zd.CFTC_HOME))
        self.assertEqual(out['order'], ['dealer', 'asset_mgr', 'lev_funds', 'other_rept', 'nonrept'])
        eur = out['markets']['eur']
        self.assertTrue(eur['in_week_file']); self.assertEqual((eur['oi'], eur['oi_chg']), (920035, -22429))
        self.assertEqual(eur['groups']['dealer'], {'long': 41113, 'short': 299193, 'spread': 5144, 'net': -258080, 'chg_net': 3374})
        self.assertEqual([out['markets']['btc']['groups']['lev_funds'][k] for k in ('long', 'short', 'spread')], [5545, 11899, 1841])
        self.assertEqual([out['markets']['eth']['groups']['dealer'][k] for k in ('long', 'short', 'spread')], [19586, 9015, 980])
        for key in ('eur', 'btc', 'eth'):
            h = out['markets'][key]['hist']
            self.assertEqual(h['dates'], _cftc_weeks('2026-09-15', 13))              # 23.06–15.09, rosnąco, 14. tydzień odcięty
            for g in out['order']:
                self.assertEqual(len(h[g]), 13); self.assertEqual(h[g][-1], out['markets'][key]['groups'][g]['net'])
        h = eur['hist']
        self.assertEqual(h['dealer'][0], -258080 + 1200); self.assertEqual(h['other_rept'][0], 7755 - 1200); self.assertEqual(h['oi'][0], 920035 + 1200)
        self.assertLess(len(json.dumps(out)), 8000)

    def test_week_file_down_uses_annual_file(self):
        import urllib.error
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**{zd.CFTC_WEEK_URL: urllib.error.URLError('timeout')})), today=_CFTC_TODAY)
        eur = out['markets']['eur']
        self.assertEqual(eur['asof'], '2026-09-15'); self.assertFalse(eur['in_week_file'])
        self.assertEqual(eur['groups']['dealer']['chg_net'], 3374)
        self.assertTrue(any(e.startswith('CFTC tydzień') for e in zd.META['errors']))

    def test_missing_cftc_change_falls_back_to_previous_report_or_stays_gap(self):
        wk = ''.join(_cftc_row(l, '2026-09-15', Change_in_Dealer_Long_All='.', Change_in_Open_Interest_All='.') if ',099741,' in l else l + '\n'
                     for l in _CFTC_WK.split('\r\n') if l)
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**{zd.CFTC_WEEK_URL: wk.encode()})), today=_CFTC_TODAY)
        self.assertEqual(out['markets']['eur']['groups']['dealer']['chg_net'], -100)   # −258080 − (−258080 + 100) z historii
        self.assertEqual(out['markets']['eur']['oi_chg'], -100)
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**{zd.CFTC_WEEK_URL: wk.encode(), zd.CFTC_YEAR_URL.format(2026): RuntimeError('503')})),
                            today=_CFTC_TODAY)
        self.assertIsNone(out['markets']['eur']['groups']['dealer']['chg_net']); self.assertIsNone(out['markets']['eur']['oi_chg'])

    def test_annual_file_down_single_point_or_previous_history_in_window(self):
        full = zd.build_cftc(fetch=_cftc_fetch(_cftc_std()), today=_CFTC_TODAY)
        zd.META['errors'].clear()
        down = {zd.CFTC_YEAR_URL.format(2026): RuntimeError('HTTP 503'), zd.CFTC_YEAR_URL.format(2025): _cftc_year(_cftc_weeks('2025-12-30', 14))}
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**down)), today=_CFTC_TODAY)
        for k in ('eur', 'btc', 'eth'):
            self.assertEqual(out['markets'][k]['hist']['dates'], ['2026-09-15'])  # nie 2025 + 15.09.2026 (dziura)
        self.assertEqual(out['markets']['eur']['groups']['lev_funds']['chg_net'], 5129)   # zmiana z kolumn CFTC, nie z historii
        self.assertTrue(any(e.startswith('CFTC rok 2026') for e in zd.META['errors']))
        prev = json.loads(json.dumps(full))
        for m in prev['markets'].values():                                           # poprzedni plik: historia do 08.09
            for f in list(m['hist']):
                m['hist'][f] = m['hist'][f][:-1]
        prev['markets']['btc']['hist']['dates'][0] = '2026-13-45'                   # zła data i nie-liczba w poprzednim pliku = pominięte
        prev['markets']['btc']['hist']['dealer'][1] = 'x'
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**down)), today=_CFTC_TODAY, prev=prev)
        h = out['markets']['eur']['hist']
        self.assertEqual(h['dates'], _cftc_weeks('2026-09-15', 13)); self.assertEqual(h['dealer'], full['markets']['eur']['hist']['dealer'])
        b = out['markets']['btc']['hist']
        self.assertEqual(len(b['dates']), 12); self.assertIsNone(b['dealer'][0])
        old = json.loads(json.dumps(prev))
        for m in old['markets'].values():
            m['hist']['dates'] = [d.replace('2026-', '2025-') for d in m['hist']['dates']]
        out = zd.build_cftc(fetch=_cftc_fetch(_cftc_std(**down)), today=_CFTC_TODAY, prev=old)
        self.assertEqual(out['markets']['eth']['hist']['dates'], ['2026-09-15'])   # stara historia spoza 90 dni odrzucona

    def test_january_uses_previous_year_file(self):
        import urllib.error
        src = {zd.CFTC_WEEK_URL: urllib.error.URLError('down'), zd.CFTC_YEAR_URL.format(2025): _cftc_year(_cftc_weeks('2025-12-30', 15))}
        out = zd.build_cftc(fetch=_cftc_fetch(src), today=datetime.date(2026, 1, 2))
        h = out['markets']['btc']['hist']
        self.assertEqual(h['dates'], _cftc_weeks('2025-12-30', 13)); self.assertEqual(out['asof'], '2025-12-30')
        self.assertTrue(any('CFTC rok 2026: HTTP 404' in e for e in zd.META['errors']))

    def test_missing_market_is_none_or_previous_state_up_to_35_days(self):
        wk = '\r\n'.join(l for l in _CFTC_WK.split('\r\n') if ',133741,' not in l)
        src = _cftc_std(**{zd.CFTC_WEEK_URL: wk.encode(), zd.CFTC_YEAR_URL.format(2026): _cftc_year(_cftc_weeks('2026-09-15', 14), skip=('133741',))})
        out = zd.build_cftc(fetch=_cftc_fetch(src), today=_CFTC_TODAY)
        self.assertIsNone(out['markets']['btc']); self.assertIsNotNone(out['markets']['eur'])
        self.assertTrue(any('brak rynku 133741' in e for e in zd.META['errors']))
        prev = {'at': '2026-09-20T00:00:00+00:00', 'markets': {'btc': {'asof': '2026-09-08', 'oi': 21083}}}
        out = zd.build_cftc(fetch=_cftc_fetch(src), today=_CFTC_TODAY, prev=prev)
        self.assertEqual(out['markets']['btc'], {'asof': '2026-09-08', 'oi': 21083, 'kept': True})
        prev['markets']['btc']['asof'] = '2026-08-18'                                # 37 dni — za stare, brak zamiast starego stanu
        self.assertIsNone(zd.build_cftc(fetch=_cftc_fetch(src), today=_CFTC_TODAY, prev=prev)['markets']['btc'])
        prev['markets']['btc']['asof'] = '2026-13-01'
        self.assertIsNone(zd.build_cftc(fetch=_cftc_fetch(src), today=_CFTC_TODAY, prev=prev)['markets']['btc'])

    def test_nothing_fetched_raises_even_with_previous_file(self):
        import urllib.error
        down = {zd.CFTC_WEEK_URL: urllib.error.URLError('down'), zd.CFTC_YEAR_URL.format(2026): RuntimeError('503'),
                zd.CFTC_YEAR_URL.format(2025): RuntimeError('503')}
        with self.assertRaises(RuntimeError):
            zd.build_cftc(fetch=_cftc_fetch(down), today=_CFTC_TODAY)
        prev = {'at': '2026-09-19T00:00:00+00:00', 'markets': {k: {'asof': '2026-09-15', 'oi': 1} for k in ('eur', 'btc', 'eth')}}
        with self.assertRaises(RuntimeError):                                        # tylko stare stany ⇒ main zachowa stary plik i jego „at”
            zd.build_cftc(fetch=_cftc_fetch(down), today=_CFTC_TODAY, prev=prev)
        self.assertTrue(all(e.startswith('CFTC') for e in zd.META['errors']))


class MainFlowCftc(unittest.TestCase):
    """v50: młody poprzedni cftc.json (< 6 h) → bez zapytań; awaria → poprzedni plik zostaje, błąd „CFTC: …” w meta."""
    ENV = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': '', 'FINNHUB_KEY': '', 'TWELVEDATA_KEY': '', 'COINMARKETCAP_KEY': '', 'FRED_KEY': ''}

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.ps = [mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj))]
        self.ps += [mock.patch.object(zd, f, side_effect=RuntimeError('offline')) for f in ('build_instytucje', 'build_krypto', 'build_tic')]
        self.ps += [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.ps]

    def tearDown(self):
        [p.stop() for p in self.ps]

    def _main(self, prev, build):
        with mock.patch.dict(os.environ, self.ENV, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'cftc' else None), \
             mock.patch.object(zd, 'build_cftc', build) as b:
            zd.main()
        return b

    def test_young_previous_file_is_reused_without_requests(self):
        prev = {'at': _iso(5 * 60), 'asof': '2026-09-15', 'markets': {}}
        self._main(prev, mock.Mock(side_effect=AssertionError('bez zapytań')))
        self.assertIs(self.saved['cftc'], prev); self.assertEqual(zd.META['ok']['cftc'], 'cached')

    def test_older_file_is_rebuilt_with_previous_passed_in(self):
        prev = {'at': _iso(7 * 60), 'asof': '2026-09-08', 'markets': {}}
        new = {'at': _iso(0), 'asof': '2026-09-15', 'markets': {}}
        b = self._main(prev, mock.Mock(return_value=new))
        b.assert_called_once_with(prev=prev)
        self.assertIs(self.saved['cftc'], new); self.assertIs(zd.META['ok']['cftc'], True)

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': _iso(7 * 60), 'asof': '2026-09-08', 'markets': {}}
        self._main(prev, mock.Mock(side_effect=RuntimeError('żaden rynek nie ma danych')))
        self.assertIs(self.saved['cftc'], prev); self.assertIs(zd.META['ok']['cftc'], False)
        self.assertIn('CFTC: żaden rynek nie ma danych', zd.META['errors'])
        self.saved.clear(); zd.META['errors'].clear()
        self._main(None, mock.Mock(side_effect=RuntimeError('x')))                      # bez poprzedniego pliku: nic nie zapisujemy
        self.assertNotIn('cftc', self.saved); self.assertIn('CFTC: x', zd.META['errors'])


def _cm_row(asset, day, i, o, iu, ou, s, su, status='flash'):
    r = {'asset': asset, 'time': day + 'T00:00:00.000000000Z'}
    for m, v in (('FlowInExNtv', i), ('FlowOutExNtv', o), ('FlowInExUSD', iu), ('FlowOutExUSD', ou),
                 ('SplyExNtv', s), ('SplyExUSD', su)):
        if v is not None:
            r[m] = v; r[m + '-status'] = status; r[m + '-status-time'] = day + 'T02:00:00.000000000Z'
    return r


def _cm_series(asset, last='2026-09-23', n=35, skip=(), status='flash'):
    """n dni rosnąco: wpływ 10, wypływ 12 (netto −2 dziennie), zapas 1000 − 2·k, USD ×100 (teksty jak u dostawcy)."""
    d0 = datetime.date.fromisoformat(last)
    rows = []
    for k in range(n):
        day = (d0 - datetime.timedelta(days=n - 1 - k)).isoformat()
        if day in skip:
            continue
        rows.append(_cm_row(asset, day, '10.0', '12.0', '1000.5', '1200.5', str(1000 - 2 * k), str((1000 - 2 * k) * 100), status))
    return rows


# Prawdziwe odpowiedzi Coin Metrics Community z 24.09.2026 (8 ostatnich dni BTC i ETH, teksty bez zmian)
CM_REAL = {
    'btc': [
        ('2026-09-16', '23005.0435735', '25111.95609172', '1750278397.87302521799362345', '1910577310.373092978573929644', '2715643.25254517', '206612593715.522673197628736459'),
        ('2026-09-17', '21766.37576865', '22768.27672937', '1662666010.15348040373565032', '1739198120.534876233151184016', '2717241.34934502', '207561648340.492287723402723936'),
        ('2026-09-18', '26956.79244258', '28008.10103786', '2181994790.729968312846095348', '2267092076.812386564929669316', '2718781.15287739', '220069443549.326947134534782334'),
        ('2026-09-19', '13920.30109519', '15406.49176817', '1131196317.767704870377350018', '1251967657.789695729333835774', '2718047.40416887', '220874907380.868559276402635314'),
        ('2026-09-20', '12118.44914273', '12143.14716736', '983954376.302886483419629726', '985959726.091007174726671232', '2721135.94115925', '220941936239.83360304532696735'),
        ('2026-09-21', '39392.93389089', '43259.91376965', '3407697475.40797693418007915', '3742211721.20654441548406775', '2719494.22011218', '235250657235.4082620030970023'),
        ('2026-09-22', '26788.45880433', '43695.27229361', '2309306372.292639122668101195', '3766762824.384114662172450315', '2705076.07472788', '233191818257.78390678652978102'),
        ('2026-09-23', '22387.64642336', '32444.54357449', '1890285676.750714654929787264', '2739432937.603468979066092076', '2695208.79361638', '227568118688.422059403028163912'),
    ],
    'eth': [
        ('2026-09-16', '289382.073310989575481583', '212743.194353739073070714', '698485929.04586564440410663348219881565', '513501496.6758600223263853703466012027', '15388706.801086797410188754', '37143956582.3421258144738703656887627247'),
        ('2026-09-17', '267625.150138587144779647', '240294.989972524729939119', '654681864.07411971396984760189458268627', '587825067.56715277754633741250812832579', '15416030.460658763593125918', '37711685741.72171387332230353704504084038'),
        ('2026-09-18', '356672.792475764516139621', '212648.878259904896237837', '931833389.97422577397346811150449431467', '555560528.53851446633457874501080026499', '15560044.485571174299546947', '40651738251.45286313523098914606477319469'),
        ('2026-09-19', '110734.936450477785281349', '117829.794546268749835735', '291556129.28182150657257360720754134489', '310236316.67812432259804510538095462835', '15552943.686290538048104223', '40949642501.85785452245489574737288447003'),
        ('2026-09-20', '92560.011058430166140539', '126968.139877963660133432', '244515137.73109773031821180382826407923', '335410852.42657819793719839574007359224', '15518530.952890564413554298', '40995195332.62570146917542426933120645386'),
        ('2026-09-21', '369833.264598950719169977', '407638.521730416910587144', '1025844852.64772373878105520862139592522', '1130709212.19470504710965456670873458384', '15480710.367535076292751177', '42940450646.28609433714037931464559775722'),
        ('2026-09-22', '166693.231624719601151318', '230825.920339214647048367', '459231641.12677258814244778721651149796', '635914039.09320861072150661229549779274', '15416565.793280767156723457', '42471879276.57467950330200650882065335254'),
        ('2026-09-23', '167730.112637279856076631', '241843.073484394985997244', '450172921.29511826612335951381361423947', '649085612.43797930337580029021565884428', '15342440.16922408408424477', '41177764696.9759050596155967026849261849'),
    ],
}


class CoinMetrics(unittest.TestCase):
    """v50: Coin Metrics Community — wpłaty/wypłaty BTC i ETH na giełdy, zapas na giełdach; brak = None, nigdy 0."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_url_is_keyless_one_request_for_both_assets(self):
        self.assertTrue(zd.CM_URL.startswith('https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?'))
        for m in ('FlowInExNtv', 'FlowOutExNtv', 'FlowInExUSD', 'FlowOutExUSD', 'SplyExNtv', 'SplyExUSD', 'assets=btc,eth',
                  'frequency=1d', 'limit_per_asset=36', 'paging_from=end', 'ignore_unsupported_errors=true'):
            self.assertIn(m, zd.CM_URL)
        self.assertNotIn('api_key', zd.CM_URL)

    def test_sums_net_and_supply_change(self):
        out = zd.parse_cm({'data': _cm_series('btc') + _cm_series('eth')})
        b = out['assets']['btc']
        self.assertEqual(out['asof'], '2026-09-23'); self.assertEqual(b['asof'], '2026-09-23'); self.assertEqual(b['status'], 'flash')
        self.assertEqual(len(b['d']), 35); self.assertEqual(b['d'][0][0], '2026-08-20'); self.assertEqual(b['d'][-1][0], '2026-09-23')
        self.assertEqual(b['last'], {'in': 10.0, 'out': 12.0, 'net': -2.0, 'in_usd': 1000, 'out_usd': 1200, 'net_usd': -200,
                                     'sply': 932.0, 'sply_usd': 93200})
        self.assertEqual(b['sum7'], {'in': 70.0, 'out': 84.0, 'net': -14.0, 'in_usd': 7004, 'out_usd': 8404, 'net_usd': -1400})
        self.assertEqual(b['sum30']['net'], -60.0)
        self.assertEqual(b['sply_ch7'], {'ntv': -14.0, 'pct': round(-14 / 946 * 100, 2)})
        self.assertEqual(b['sply_ch30']['ntv'], -60.0)
        self.assertEqual(b['missing'], 0); self.assertIsNone(b['pending'])
        self.assertEqual(out['cols'], ['date', 'in', 'out', 'net', 'in_usd', 'out_usd', 'net_usd', 'sply', 'sply_usd'])
        self.assertEqual(out['license'], 'CC BY-NC 4.0')
        self.assertTrue(out['attribution'].startswith('Source: Coin Metrics Community Network Data'))
        self.assertIn('https://coinmetrics.io', out['attribution']); self.assertIn(zd.CM_LICENSE_URL, out['attribution'])

    def test_missing_day_is_none_never_zero_and_breaks_only_its_windows(self):
        out = zd.parse_cm({'data': _cm_series('btc', skip=('2026-09-20',)) + _cm_series('eth')})
        b = out['assets']['btc']
        gap = [r for r in b['d'] if r[0] == '2026-09-20'][0]
        self.assertEqual(gap[1:], [None] * 8)
        self.assertEqual(b['missing'], 1)
        self.assertIsNone(b['sum7']['in']); self.assertIsNone(b['sum30']['net'])
        self.assertEqual(b['sply_ch7']['ntv'], -14.0, 'zmiana zapasu liczy tylko dwa końce okna')
        self.assertEqual(out['assets']['eth']['sum7']['net'], -14.0, 'luka BTC nie psuje ETH')

    def test_one_side_missing_gives_none_net_but_keeps_other_values(self):
        rows = _cm_series('btc')
        del rows[-1]['FlowOutExUSD']
        rows[-1]['FlowOutExNtv'] = 'nan'
        rows[-2]['FlowInExNtv'] = '-5'
        b = zd.parse_cm({'data': rows})['assets']['btc']
        self.assertIsNone(b['last']['out']); self.assertIsNone(b['last']['net']); self.assertIsNone(b['last']['net_usd'])
        self.assertEqual(b['last']['in_usd'], 1000); self.assertEqual(b['last']['sply'], 932.0)
        self.assertIsNone(b['d'][-2][1], 'ujemny przepływ to błąd dostawcy → None')
        self.assertIsNone(b['sum7']['out']); self.assertIsNone(b['sum7']['in'])

    def test_one_asset_missing_keeps_the_other_and_reports(self):
        out = zd.parse_cm({'data': _cm_series('eth')})
        self.assertIsNone(out['assets']['btc']); self.assertEqual(out['assets']['eth']['asof'], '2026-09-23')
        self.assertIn('Coin Metrics: brak dni dla btc', zd.META['errors'])

    def test_different_last_days_give_a_range(self):
        out = zd.parse_cm({'data': _cm_series('btc', last='2026-09-22') + _cm_series('eth')})
        self.assertEqual(out['asof'], '2026-09-22 – 2026-09-23')

    def test_reviewed_status_and_mixed_status(self):
        self.assertEqual(zd.parse_cm({'data': _cm_series('btc', status='reviewed')})['assets']['btc']['status'], 'reviewed')
        rows = _cm_series('btc', status='reviewed'); rows[-1]['SplyExUSD-status'] = 'flash'
        self.assertEqual(zd.parse_cm({'data': rows})['assets']['btc']['status'], 'flash')

    def test_errors_and_empty_answers_raise(self):
        with self.assertRaises(RuntimeError):
            zd.parse_cm({'error': {'type': 'bad_parameter', 'message': "Bad parameter 'metrics'."}})
        with self.assertRaises(RuntimeError):
            zd.parse_cm({'data': []})
        with self.assertRaises(RuntimeError):
            zd.parse_cm([])
        with self.assertRaises(RuntimeError):
            zd.parse_cm({'data': [{'asset': 'btc', 'time': 'wczoraj', 'FlowInExNtv': '1'}]})

    def test_build_cm_uses_one_keyless_request(self):
        seen = []
        def get_json(url, headers=None):
            seen.append((url, headers)); return {'data': _cm_series('btc') + _cm_series('eth')}
        with mock.patch.object(zd, 'get_json', get_json):
            out = zd.build_cm()
        self.assertEqual(seen, [(zd.CM_URL, None)]); self.assertEqual(sorted(out['assets']), ['btc', 'eth'])

    def test_newest_day_still_publishing_falls_back_to_last_full_day(self):
        rows = _cm_series('btc') + _cm_series('eth')
        rows.append(_cm_row('btc', '2026-09-24', '11.0', '9.0', None, None, '930', None))   # natywne już są, USD jeszcze nie
        out = zd.parse_cm({'data': rows})
        b = out['assets']['btc']
        self.assertEqual(out['asof'], '2026-09-23'); self.assertEqual(b['asof'], '2026-09-23'); self.assertEqual(b['pending'], '2026-09-24')
        self.assertEqual(b['d'][-1][0], '2026-09-23'); self.assertEqual(len(b['d']), 35); self.assertEqual(b['missing'], 0)
        self.assertEqual(b['last']['net_usd'], -200); self.assertEqual(b['sum7']['in_usd'], 7004)
        self.assertIsNone(out['assets']['eth']['pending'])

    def test_api_window_has_no_false_gap_while_newest_day_is_publishing(self):
        """Coin Metrics zwraca limit_per_asset NAJNOWSZYCH wierszy, także niepełny dzień w publikacji (ok. 02–03 UTC).
        Po cofnięciu do ostatniego pełnego dnia okno 35 dni ma być pełne — brak dnia to tylko prawdziwa luka u dostawcy."""
        lim = int(zd.CM_URL.split('limit_per_asset=')[1].split('&')[0])
        rows = []
        for a in ('btc', 'eth'):
            r = _cm_series(a, last='2026-09-23', n=60) + [_cm_row(a, '2026-09-24', '11.0', '9.0', None, None, '930', None)]
            rows += r[-lim:]                                   # tyle wierszy odda API (paging_from=end)
        b = zd.parse_cm({'data': rows})['assets']['btc']
        self.assertEqual(b['pending'], '2026-09-24'); self.assertEqual(b['asof'], '2026-09-23')
        self.assertEqual(b['missing'], 0, 'najstarszy dzień okna nie może być fałszywą luką')
        self.assertEqual(b['d'][0][0], '2026-08-20'); self.assertIsNotNone(b['d'][0][1]); self.assertIsNotNone(b['sply_ch30']['ntv'])
        rows = [x for a in ('btc', 'eth') for x in _cm_series(a, n=60)[-lim:]]   # zwykła pora: wszystkie dni pełne
        b = zd.parse_cm({'data': rows})['assets']['btc']
        self.assertEqual(b['missing'], 0); self.assertEqual(len(b['d']), 35); self.assertIsNone(b['pending'])

    def test_partial_day_after_a_gap_is_not_hidden(self):
        rows = _cm_series('btc', last='2026-09-21')
        rows.append(_cm_row('btc', '2026-09-23', '11.0', '9.0', None, None, '930', None))
        b = zd.parse_cm({'data': rows})['assets']['btc']
        self.assertEqual(b['asof'], '2026-09-23'); self.assertIsNone(b['pending']); self.assertIsNone(b['last']['in_usd'])
        self.assertEqual(b['last']['net'], 2.0); self.assertIsNone(b['sum7']['in'], 'dzień 2026-09-22 brak → suma null, nie zero')

    def test_impossible_date_is_skipped_not_crash(self):
        rows = _cm_series('btc') + [_cm_row('btc', '2026-02-30', '1', '1', '1', '1', '1', '1')]
        self.assertEqual(zd.parse_cm({'data': rows})['assets']['btc']['asof'], '2026-09-23')

    def test_paged_answer_is_reported(self):
        zd.parse_cm({'data': _cm_series('btc') + _cm_series('eth'), 'next_page_token': 'abc'})
        self.assertTrue(any(e.startswith('Coin Metrics') and 'stron' in e for e in zd.META['errors']))

    def test_whole_metric_missing_is_reported_and_left_null(self):
        rows = _cm_series('btc') + _cm_series('eth')
        for r in rows:
            r.pop('SplyExUSD', None)
        out = zd.parse_cm({'data': rows})
        self.assertIn('Coin Metrics: brak metryki SplyExUSD w odpowiedzi', zd.META['errors'])
        b = out['assets']['btc']
        self.assertEqual(b['asof'], '2026-09-23'); self.assertIsNone(b['last']['sply_usd']); self.assertEqual(b['last']['sply'], 932.0)

    def test_real_answer_24_09_2026_net_from_unrounded_values(self):
        rows = [_cm_row(a, *r) for a in ('btc', 'eth') for r in CM_REAL[a]]
        out = zd.parse_cm({'data': rows})
        b, e = out['assets']['btc'], out['assets']['eth']
        self.assertEqual(out['asof'], '2026-09-23'); self.assertEqual(b['status'], 'flash'); self.assertIsNone(b['pending'])
        self.assertEqual(b['last']['in'], 22387.65); self.assertEqual(b['last']['out'], 32444.54)
        self.assertEqual(b['last']['net'], -10056.9, 'netto z liczb niezaokrąglonych (22387.65 − 32444.54 dałoby −10056.89)')
        self.assertEqual(b['last']['net_usd'], -849147261); self.assertEqual(b['last']['sply'], 2695208.79)
        self.assertEqual(b['sum7']['net'], -34394.79); self.assertEqual(b['sply_ch7'], {'ntv': -20434.46, 'pct': -0.75})
        self.assertIsNone(b['sum30']['net'], 'w próbce 8 dni — suma 30 dni to brak, nie zero')
        self.assertEqual(b['missing'], 27)
        self.assertEqual(e['last']['net'], -74112.96); self.assertEqual(e['sum7']['net'], -46199.82)
        self.assertEqual(e['sply_ch7'], {'ntv': -46266.63, 'pct': -0.3})


class MainFlowCm(unittest.TestCase):
    """v50: blok Coin Metrics w main() — pamięć 60 min, przy awarii poprzedni plik i META ok False, błąd z prefiksem."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.patches = [mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj))]
        for fn in ('build_instytucje', 'build_krypto', 'build_tic'):
            self.patches.append(mock.patch.object(zd, fn, side_effect=RuntimeError('offline')))
        for fn in ('build_aukcje', 'build_bis', 'build_cftc', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider'):   # pozostałe źródła v50 (mogą jeszcze nie istnieć); v95.3: bez pobierania funduszy i surowców w teście
            self.patches.append(mock.patch.object(zd, fn, side_effect=RuntimeError('offline'), create=True))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _run(self, prev, build):
        env = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'cm' else None), \
             mock.patch.object(zd, 'build_cm', build):
            zd.main()

    def test_young_previous_file_is_reused_without_request(self):
        prev = {'at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(), 'assets': {}}
        self._run(prev, mock.Mock(side_effect=AssertionError('bez zapytań')))
        self.assertIs(self.saved['cm'], prev); self.assertEqual(zd.META['ok']['cm'], 'cached')

    def test_failure_keeps_previous_and_reports(self):
        prev = {'at': '2026-09-24T10:00:00+00:00', 'assets': {}}
        self._run(prev, mock.Mock(side_effect=RuntimeError('HTTP Error 429')))
        self.assertIs(self.saved['cm'], prev); self.assertIs(zd.META['ok']['cm'], False)
        self.assertIn('Coin Metrics: HTTP Error 429', zd.META['errors'])

    def test_parser_error_is_not_prefixed_twice(self):
        self._run(None, lambda: zd.parse_cm({'data': []}))
        self.assertNotIn('cm', self.saved); self.assertIs(zd.META['ok']['cm'], False)
        self.assertIn('Coin Metrics: żadne aktywo nie ma danych', zd.META['errors'])
        self.assertFalse(any(e.startswith('Coin Metrics: Coin Metrics') for e in zd.META['errors']))

    def test_fresh_data_is_saved(self):
        self._run(None, lambda: zd.parse_cm({'data': _cm_series('btc') + _cm_series('eth')}))
        self.assertEqual(self.saved['cm']['asof'], '2026-09-23'); self.assertIs(zd.META['ok']['cm'], True)



class FedCustodyV50(unittest.TestCase):
    """v50: Fed H.4.1 — papiery w depozycie dla zagranicznych instytucji oficjalnych (FRED). Liczby = H.4.1 z 17.09.2026, mln USD."""
    ROWS = {   # data: (WSEFINTL1, WMTSECL1, WFASECL1, WSEFINOL) — prawdziwe stany środowe
        '2026-09-16': (2884717, 2608821, 202071, 73825),
        '2026-09-09': (2865365, 2590095, 201252, 74017),
        '2026-08-19': (2864974, 2586171, 204466, 74338),
        '2025-09-17': (3119250, 2792652, 247489, 79109),
        '2025-09-10': (3129862, 2802270, 248101, 79491),
    }
    IDS = ['WSEFINTL1', 'WMTSECL1', 'WFASECL1', 'WSEFINOL']

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def _json(self, sid, rows=None, drop=()):
        i = self.IDS.index(sid)
        return {'observations': [{'date': d, 'value': ('.' if (sid, d) in drop else str(v[i]) + '.0')}
                                 for d, v in sorted((rows or self.ROWS).items(), reverse=True)]}

    def _series(self, rows=None, drop=()):
        out = {}
        for sid in self.IDS:
            try:
                out[sid] = zd.parse_fred(self._json(sid, rows, drop), sid)
            except RuntimeError:
                pass
        return out

    def test_series_are_fed_board_h41_weekly_in_millions(self):
        self.assertEqual(sorted(zd.FRED_CUSTODY), sorted(self.IDS))
        for sid in self.IDS:
            self.assertEqual((zd.FRED_SERIES[sid]['unit'], zd.FRED_SERIES[sid]['freq']), ('mln USD', 'W'))
            self.assertIn('H.4.1', zd.FRED_SERIES[sid]['name'])

    def test_summary_matches_h41_release_of_2026_09_17(self):
        s = zd.custody_summary(self._series())
        self.assertEqual((s['asof'], s['total'], s['unit']), ('2026-09-16', 2884717.0, 'mln USD'))
        self.assertEqual((s['ust'], s['agency'], s['other']), (2608821.0, 202071.0, 73825.0))
        self.assertEqual((s['d1w'], s['d4w'], s['d52w']), (19352.0, 19743.0, -234533.0))   # zmiana STANU NA ŚRODĘ
        self.assertEqual((s['ust_d1w'], s['ust_d52w']), (18726.0, -183831.0))
        self.assertEqual(s['ust_share_pct'], 90.4); self.assertIs(s['parts_ok'], True)
        self.assertEqual(s['lo52'], ['2026-08-19', 2864974.0]); self.assertEqual(s['hi52'], ['2025-09-17', 3119250.0])

    def test_missing_week_is_none_not_an_older_week_and_not_zero(self):
        rows = {d: v for d, v in self.ROWS.items() if d != '2026-09-09'}
        s = zd.custody_summary(self._series(rows))
        self.assertIsNone(s['d1w']); self.assertIsNone(s['ust_d1w']); self.assertEqual(s['d4w'], 19743.0)

    def test_missing_part_is_none_and_parts_not_summing_are_flagged(self):
        s = zd.custody_summary(self._series(drop={('WFASECL1', '2026-09-16')}))
        self.assertIsNone(s['agency']); self.assertIsNone(s['parts_ok']); self.assertEqual(s['total'], 2884717.0)
        rows = dict(self.ROWS); rows['2026-09-16'] = (2884717, 2608821, 202071, 63825)
        self.assertIs(zd.custody_summary(self._series(rows))['parts_ok'], False)

    def test_no_total_series_gives_none_and_nan_is_ignored(self):
        self.assertIsNone(zd.custody_summary({})); self.assertIsNone(zd.custody_summary(None))
        self.assertIsNone(zd.custody_summary({'WMTSECL1': zd.parse_fred(self._json('WMTSECL1'), 'WMTSECL1')}))
        ser = self._series(); ser['WSEFINTL1']['d'].append(['2026-09-23', float('nan')])
        self.assertEqual(zd.custody_summary(ser)['asof'], '2026-09-16')

    def test_build_fred_adds_custody_and_a_summary_error_does_not_stop_fred(self):
        zd.SECRETS[:] = ['TAJNY-FRED']
        def get_json(url, headers=None):
            sid = url.split('series_id=')[1].split('&')[0]
            return self._json(sid) if sid in self.IDS else {'observations': [{'date': '2026-09-16', 'value': '5'}]}
        try:
            with mock.patch.object(zd, 'get_json', get_json), mock.patch.object(zd.time, 'sleep', lambda s: None):
                out = zd.build_fred('TAJNY-FRED')
                self.assertEqual(out['custody']['total'], 2884717.0); self.assertEqual(len(out['series']), 8)
                with mock.patch.object(zd, 'custody_summary', side_effect=ValueError('zły kształt TAJNY-FRED')):
                    out = zd.build_fred('TAJNY-FRED')
        finally:
            zd.SECRETS[:] = []
        self.assertIsNone(out['custody']); self.assertEqual(len(out['series']), 8)
        self.assertIn('FRED custody: zły kształt ***', zd.META['errors'])


def _imf_sdmx(series, periods, dims=None):
    """Minimalna odpowiedź SDMX-JSON 2.0 w kształcie API MFW 3.0 (IL, 24.09.2026)."""
    dims = dims or [('COUNTRY', ['BRA', 'CHN', 'TWN']), ('INDICATOR', ['RXF11FX_REVS', 'RXF11_REVS', 'TRGMV_REVS']),
                    ('UNIT', ['USD']), ('FREQUENCY', ['M'])]
    return {'meta': {}, 'data': {
        'dataSets': [{'structure': 0, 'action': 'Replace', 'series': {k: {'attributes': [0, None, 'true'], 'observations': {
            str(i): [v, None, 0, None] for i, v in obs.items()}} for k, obs in series.items()}}],
        'structures': [{'dimensions': {
            'series': [{'id': n, 'keyPosition': p, 'values': [{'id': x} for x in vals]} for p, (n, vals) in enumerate(dims)],
            'observation': [{'id': 'TIME_PERIOD', 'keyPosition': 4, 'values': [{'value': x} for x in periods]}]},
            'attributes': {'series': [{'id': 'SCALE', 'values': [{'id': '6'}]}]}}]}}


class ImfRezerwyV50(unittest.TestCase):
    """v50: rezerwy walutowe z MFW (International Liquidity) — mld USD, każdy kraj z własnym miesiącem, brak = brak."""
    PER = ['2026-M05', '2026-M06', '2025-M06', '2026-M08', '2026-M07', '2025-M08', '2026-M04', '2025-M04']   # celowo nie po kolei
    MINI = {   # prawdziwe wartości (USD) z odpowiedzi IL 24.09.2026
        '1:2:0:0': {1: '3786110832113.452', 0: '3850222574625.652', 2: '3627580370629.294'},   # CHN razem (złoto rynkowo)
        '1:1:0:0': {1: '3482385620113.452', 0: '3509458162625.652'},                          # CHN bez złota
        '1:0:0:0': {1: '3416262000000', 0: '3442238000000'},                                  # CHN waluty
        '0:2:0:0': {3: '373354596341.5881', 4: '369648992876.5459', 5: None},                 # BRA razem; 2025-08 = null
        '0:0:0:0': {3: '324148312609.55', 4: 'NaN'},                                          # BRA waluty; NaN = brak
        '2:0:0:0': {6: '602488000000'},                                                       # TWN tylko waluty, bez sumy
    }

    def test_url_is_keyless_and_asks_for_all_countries_and_indicators(self):
        u = zd.IMF_IL_URL
        self.assertTrue(u.startswith('https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/IL/+/'))
        self.assertNotIn('key', u.lower().replace('lastnobservations', ''))
        for c in ('CHN', 'JPN', 'IND', 'SAU', 'KOR', 'CHE', 'BRA', 'TWN', 'TRGMV_REVS', 'RXF11_REVS', 'RXF11FX_REVS'):
            self.assertIn(c, u)
        self.assertTrue(u.endswith('.USD.M?lastNObservations=13'))

    def test_period_formats(self):
        self.assertEqual(zd._imf_month('2026-M06'), '2026-06'); self.assertEqual(zd._imf_month('2026-06'), '2026-06')
        self.assertIsNone(zd._imf_month('2026-Q2')); self.assertIsNone(zd._imf_month('2026-M13')); self.assertIsNone(zd._imf_month(None))
        self.assertEqual(zd._imf_month_add('2026-01', -1), '2025-12'); self.assertEqual(zd._imf_month_add('2026-06', -12), '2025-06')

    def test_parser_orders_periods_and_skips_null_and_nan(self):
        ser = zd.parse_imf_sdmx(_imf_sdmx(self.MINI, self.PER))
        self.assertEqual(ser[('CHN', 'TRGMV_REVS', 'USD', 'M')][0], ['2025-06', 3627580370629.294])
        self.assertEqual([r[0] for r in ser[('BRA', 'TRGMV_REVS', 'USD', 'M')]], ['2026-07', '2026-08'])
        self.assertEqual(ser[('BRA', 'RXF11FX_REVS', 'USD', 'M')], [['2026-08', 324148312609.55]])

    def test_reserves_in_billions_each_country_with_own_month(self):
        r = zd.parse_rezerwy(_imf_sdmx(self.MINI, self.PER))
        chn, bra = r['countries']['CHN'], r['countries']['BRA']
        self.assertEqual((chn['asof'], chn['total'], chn['fx'], chn['ex_gold'], chn['gold']), ('2026-06', 3786.1, 3416.3, 3482.4, 303.7))
        self.assertEqual((chn['d1m'], chn['d12m'], chn['p12m']), (-64.1, 158.5, 4.4))
        self.assertEqual((bra['asof'], bra['total'], bra['fx']), ('2026-08', 373.4, 324.1))
        self.assertIsNone(bra['ex_gold']); self.assertIsNone(bra['gold']); self.assertIsNone(bra['d12m'])   # brak = brak, nie zero
        self.assertEqual(r['order'], ['CHN', 'BRA']); self.assertEqual(r['unit'], 'mld USD')
        self.assertIn('TWN', r['missing']); self.assertIn('JPN', r['missing'])
        self.assertEqual((r['asof_min'], r['asof_max']), ('2026-06', '2026-08'))
        self.assertIn('International Monetary Fund', r['src']); self.assertTrue(r['url'].startswith('https://data.imf.org/'))
        json.dumps(r, allow_nan=False)   # żadnego NaN w pliku

    def test_empty_answer_or_scaled_values_are_errors_not_zeros(self):
        empty = _imf_sdmx({}, self.PER); del empty['data']['dataSets'][0]['series']
        for bad in (empty, {'errors': [{'code': 404}]}, _imf_sdmx({'1:2:0:0': {1: '3786110.83'}, '0:2:0:0': {3: '373354.6'}}, self.PER)):
            with self.assertRaises(RuntimeError):
                zd.parse_rezerwy(bad)

    def test_build_rezerwy_sends_json_accept_header_and_stamps_time(self):
        seen = {}
        def get_json(url, headers=None):
            seen['url'], seen['headers'] = url, headers
            return _imf_sdmx(self.MINI, self.PER)
        with mock.patch.object(zd, 'get_json', get_json):
            out = zd.build_rezerwy()
        self.assertEqual(seen['url'], zd.IMF_IL_URL); self.assertEqual(seen['headers'], {'Accept': 'application/json'})
        self.assertEqual(out['at'], zd.NOW); self.assertEqual(out['countries']['CHN']['total'], 3786.1)


class MainFlowRezerwyV50(unittest.TestCase):
    ENV = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY')}

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); self.saved = {}
        self.ps = [mock.patch.object(zd, 'save', lambda name, obj: self.saved.__setitem__(name, obj))]
        self.ps += [mock.patch.object(zd, f, side_effect=RuntimeError('offline')) for f in ('build_instytucje', 'build_krypto', 'build_tic')]
        self.ps += [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_bis', 'build_cftc', 'build_cm', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in self.ps]

    def tearDown(self):
        [p.stop() for p in self.ps]

    def _main(self, prev, build):
        with mock.patch.dict(os.environ, self.ENV, clear=False), mock.patch.object(zd, 'previous', lambda name: prev if name == 'rezerwy' else None), \
             mock.patch.object(zd, 'build_rezerwy', build):
            zd.main()

    def test_young_previous_file_is_reused_without_asking_imf(self):
        prev = {'at': _iso(60), 'countries': {'CHN': {'total': 3786.1}}, 'order': ['CHN']}
        self._main(prev, mock.Mock(side_effect=AssertionError('bez zapytań do MFW')))
        self.assertIs(self.saved['rezerwy'], prev); self.assertEqual(zd.META['ok']['imf'], 'cached')

    def test_failure_keeps_previous_file_and_reports_with_mfw_prefix(self):
        prev = {'at': _iso(26 * 60), 'countries': {'CHN': {'total': 3786.1}}, 'order': ['CHN']}
        self._main(prev, mock.Mock(side_effect=RuntimeError('brak serii z wartościami')))
        self.assertIs(self.saved['rezerwy'], prev); self.assertIs(zd.META['ok']['imf'], False)
        self.assertIn('MFW rezerwy: brak serii z wartościami', zd.META['errors'])

    def test_fresh_build_is_saved(self):
        new = {'at': zd.NOW, 'countries': {}, 'order': []}
        self._main(None, mock.Mock(return_value=new))
        self.assertIs(self.saved['rezerwy'], new); self.assertIs(zd.META['ok']['imf'], True)


class StanV51(unittest.TestCase):
    """v51: poprzedni plik = nowszy z pamięci Actions i ze strony; 404 to informacja; bez świecy trwającej sesji."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear(); zd.SECRETS[:] = []

    def test_previous_takes_the_newer_of_cache_and_site(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'ceny.json'), 'w', encoding='utf-8') as f:
                json.dump({'at': '2026-09-25T08:00:00+00:00', 'src': 'pamięć'}, f)
            with mock.patch.dict(os.environ, {'CACHE_DIR': d, 'SITE_URL': 'https://x'}, clear=False), \
                 mock.patch.object(zd, 'get_json', lambda u, h=None: {'at': '2026-09-25T07:00:00+00:00', 'src': 'strona'}):
                self.assertEqual(zd.previous('ceny')['src'], 'pamięć')
            with mock.patch.dict(os.environ, {'CACHE_DIR': d, 'SITE_URL': 'https://x'}, clear=False), \
                 mock.patch.object(zd, 'get_json', lambda u, h=None: {'at': '2026-09-25T09:00:00+00:00', 'src': 'strona'}):
                self.assertEqual(zd.previous('ceny')['src'], 'strona')
            with mock.patch.dict(os.environ, {'CACHE_DIR': d, 'SITE_URL': ''}, clear=False):
                self.assertEqual(zd.previous('ceny')['src'], 'pamięć', 'awaria/brak strony — zostaje pamięć')

    def test_missing_previous_file_is_a_note_not_an_error(self):
        def nf(u, h=None):
            raise zd.urllib.error.HTTPError(u, 404, 'Not Found', None, None)
        with mock.patch.dict(os.environ, {'CACHE_DIR': '', 'SITE_URL': 'https://x'}, clear=False), mock.patch.object(zd, 'get_json', nf):
            self.assertIsNone(zd.previous('bis'))
        self.assertEqual(zd.META['errors'], []); self.assertTrue(any('bis.json' in n for n in zd.META['notes']))

    def test_running_session_candle_is_dropped_before_the_close(self):
        ny = datetime.datetime(2026, 9, 24, 15, 2)
        q = {'SPY': {'d': [['2026-09-23', 600.0, 54700000], ['2026-09-24', 601.0, 1100000]], 'asof': '2026-09-24'},
             'ASEA': {'d': [['2026-09-22', 10.0, 1], ['2026-09-23', 10.1, 1]], 'asof': '2026-09-23'}}
        self.assertEqual(zd._drop_open_session(q, ny), 1)
        self.assertEqual(q['SPY']['d'][-1][0], '2026-09-23'); self.assertEqual(q['SPY']['asof'], '2026-09-23'); self.assertEqual(q['ASEA']['asof'], '2026-09-23')
        q2 = {'SPY': {'d': [['2026-09-24', 601.0, 50000000]], 'asof': '2026-09-24'}}
        self.assertEqual(zd._drop_open_session(q2, datetime.datetime(2026, 9, 24, 16, 30)), 0, 'po zamknięciu świeca zostaje')


class StopyV52(unittest.TestCase):
    """v52: stopy banków centralnych (BIS WS_CBPOL): CSV z cudzysłowami, NaN/status M = brak, zmiany i różnica wobec Fed."""
    D = ('FREQ,REF_AREA,UNIT_MEASURE,TITLE,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n'
         'D,US,368,"Central bank policy rates - United States, daily",2026-09-19,3.625,A\n'
         'D,US,368,"Central bank policy rates - United States, daily",2026-09-22,3.875,A\n'
         'D,ID,368,"Indonesia, ""policy""",2026-09-19,5.75,A\n'
         'D,ID,368,"Indonesia",2026-09-20,NaN,M\n'
         'D,XM,368,"Euro area",2026-09-22,2.5,A\n')
    M = ('FREQ,REF_AREA,UNIT_MEASURE,TITLE,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n'
         'M,US,368,"x",2025-09,4.125,A\nM,US,368,"x",2026-07,3.625,A\nM,US,368,"x",2026-08,3.625,A\n'
         'M,XM,368,"x",2025-09,2.0,A\nM,XM,368,"x",2026-06,2.25,A\nM,XM,368,"x",2026-07,2.25,A\nM,XM,368,"x",2026-08,2.25,A\n'
         'M,ID,368,"x",2026-08,5.75,A\n')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_csv_with_quotes_and_missing_values(self):
        d = zd.parse_cbpol_csv(self.D.encode())
        self.assertEqual(d['US'], [['2026-09-19', 3.625], ['2026-09-22', 3.875]])
        self.assertEqual(d['ID'], [['2026-09-19', 5.75]], 'NaN ze statusem M pominięte, nie zero')
        with self.assertRaises(RuntimeError):
            zd.parse_cbpol_csv(b'FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\n')

    def test_summary_changes_and_spread_vs_fed(self):
        rows = zd.cbpol_summary(zd.parse_cbpol_csv(self.D.encode()), zd.parse_cbpol_csv(self.M.encode()))
        us, xm, idn = rows['US'], rows['XM'], rows['ID']
        self.assertEqual((us['rate'], us['date']), (3.875, '2026-09-22'))
        self.assertEqual(us['d12'], -0.25); self.assertEqual(us['last'], ['2026-09', 0.25]); self.assertEqual(us['vs_us'], 0)
        self.assertEqual(xm['last'], ['2026-09', 0.25]); self.assertEqual(xm['d12'], 0.5); self.assertEqual(xm['vs_us'], -1.375)
        self.assertEqual(idn['rate'], 5.75); self.assertIsNone(idn['d12'], 'brak historii 12 mies. = brak, nie zero')
        self.assertEqual((us['m_n'], idn['m_n']), (3, 1), 'v63: liczba miesięcy historii przy każdym kraju')

    def test_build_uses_daily_and_monthly_and_keeps_order(self):
        def gb(url, headers=None, timeout=60):
            return (self.D if '/D.' in url else self.M).encode()
        with mock.patch.object(zd, 'get_bytes', gb):
            out = zd.build_stopy()
        self.assertEqual(out['order'], ['US', 'XM', 'ID']); self.assertEqual(out['asof'], '2026-09-22'); self.assertEqual(out['unit'], '% rocznie')


class KursyV53(unittest.TestCase):
    """v53: średnie miesięczne kursów EBC (EXR) — do okien OECD na mapie; brak/0 = pominięte, bez USD = błąd."""
    CSV = ('KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\n'
           'EXR.M.USD.EUR.SP00.A,M,USD,EUR,SP00,A,2026-07,1.1500,A,"US dollar/Euro, ""ECB"""\n'
           'EXR.M.USD.EUR.SP00.A,M,USD,EUR,SP00,A,2026-08,1.1593095238,A,"US dollar/Euro"\n'
           'EXR.M.JPY.EUR.SP00.A,M,JPY,EUR,SP00,A,2026-08,184.1019047619,A,"Japanese yen/Euro"\n'
           'EXR.M.JPY.EUR.SP00.A,M,JPY,EUR,SP00,A,2026-07,182.0,A,"Japanese yen/Euro"\n'
           'EXR.M.TRY.EUR.SP00.A,M,TRY,EUR,SP00,A,2026-08,NaN,M,"Turkish lira/Euro"\n'
           'EXR.M.KRW.EUR.SP00.A,M,KRW,EUR,SP00,A,2026-08,0,A,"x"\n')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_parse_sorted_rounded_and_missing_skipped(self):
        m = zd.parse_exr_csv(self.CSV.encode())
        self.assertEqual(m['USD'], [['2026-07', 1.15], ['2026-08', 1.15931]])
        self.assertEqual(m['JPY'][0][0], '2026-07', 'rosnąco po miesiącu')
        self.assertNotIn('TRY', m, 'NaN = brak, nie zero'); self.assertNotIn('KRW', m, 'kurs 0 = brak')
        with self.assertRaises(RuntimeError):
            zd.parse_exr_csv(b'KEY,FREQ,CURRENCY,TIME_PERIOD,OBS_VALUE\nX,M,JPY,2026-08,184\n')

    def test_build_has_asof_from_usd_and_no_key_in_url(self):
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url); return self.CSV.encode()
        with mock.patch.object(zd, 'get_bytes', gb):
            out = zd.build_kursy()
        self.assertEqual(out['asof'], '2026-08'); self.assertIn('m', out); self.assertIn('EXR/M.USD+CAD', seen[0])
        self.assertIn('lastNObservations=15', seen[0]); self.assertNotIn('key', seen[0].lower())


class ObceV54(unittest.TestCase):
    """v54: zmierzone dzienne przepływy inwestorów zagranicznych — NSDL (Indie) i TWSE (Tajwan)."""
    NSDL = (
        '<html><body><table><tr><td colspan="8">Daily Trends in FPI Investments on 24-Sep-2026</td></tr>'
        '<tr><th>Reporting Date</th><th>Debt/Equity</th><th>Route</th><th>GP</th><th>GS</th><th>Net</th><th>Net US($) million</th><th>Conv</th></tr>'
        '<tr><td rowspan="3">23-Sep-2026</td><td rowspan="3">Equity</td><td>Stock Exchange</td><td>10.0</td><td>20.0</td><td>(10.00)</td><td>(270.67)</td><td>Rs.95.8179</td></tr>'
        '<tr><td>Primary market &amp; others</td><td>1</td><td>0</td><td>1</td><td>0.10</td></tr>'
        '<tr><td>Sub-total</td><td>11</td><td>20</td><td>(9)</td><td>(270.67)</td></tr>'
        '<tr><td>Debt-General Limit</td><td>Stock Exchange</td><td>1</td><td>1</td><td>0</td><td>(20.00)</td></tr>'
        '<tr><td>Sub-total</td><td>1</td><td>1</td><td>0</td><td>(20.00)</td></tr>'
        '<tr><td>Debt-VRR</td><td>Stock Exchange</td><td>1</td><td>1</td><td>0</td><td>(4.91)</td></tr>'
        '<tr><td>Sub-total</td><td>1</td><td>1</td><td>0</td><td>(4.91)</td></tr>'
        '<tr><td>Total</td><td>1</td><td>1</td><td>0</td><td>(290.42)</td></tr>'
        '<tr><td rowspan="2">24-Sep-2026</td><td>Equity</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>178.14</td><td>Rs.95.7310</td></tr>'
        '<tr><td>Sub-total</td><td>1</td><td>1</td><td>1</td><td>754.19</td></tr>'
        '<tr><td>Hybrid</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>(0.19)</td></tr>'
        '<tr><td>Sub-total</td><td>1</td><td>1</td><td>1</td><td>(0.19)</td></tr>'
        '<tr><td>Total</td><td>1</td><td>1</td><td>1</td><td>1,806.15</td></tr>'
        '<tr><td>Reporting Date</td><td>Derivative Products</td></tr>'
        '<tr><td>25-Sep-2026</td><td>Index Futures</td><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td></tr>'
        '</table></body></html>')

    @staticmethod
    def tw(date, fx='-32,964,613,655', stat='OK'):
        if stat != 'OK':
            return {'stat': 'No Data!'}
        return {'stat': 'OK', 'date': date.replace('-', ''), 'data': [
            ['Dealers (Proprietary)', '1', '1', '4,235,536,088'], ['Dealers (Hedge)', '1', '1', '-2,897,105,667'],
            ['Securities Investment Trust Companies', '1', '1', '-12,823,263,300'],
            ['Foreign Investors include Mainland Area Investors(Foreign Dealers excluded)', '1', '1', fx],
            ['Foreign Dealers', '0', '0', '0'], ['Total', '1', '1', '-44,449,446,534']]}

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_nsdl_parse_parentheses_debt_sum_and_stop_at_derivatives(self):
        rows = zd.parse_nsdl_html(self.NSDL)
        self.assertEqual(rows[0], ['2026-09-23', -270.67, -24.91, None, -290.42, 95.8179])
        self.assertEqual(rows[1], ['2026-09-24', 754.19, None, -0.19, 1806.15, 95.731], 'brak długu = None, nie zero')
        self.assertEqual(len(rows), 2, 'tabela instrumentów pochodnych pominięta')
        with self.assertRaises(RuntimeError):
            zd.parse_nsdl_html('<table><tr><td>nic</td></tr></table>')

    def test_twse_parse_units_and_no_session(self):
        self.assertEqual(zd.parse_twse(self.tw('2026-09-24')), ['2026-09-24', -32964.6, -12823.3, 1338.4, -44449.4])
        self.assertIsNone(zd.parse_twse(self.tw('2026-09-20', stat='x')), 'weekend / święto = brak, nie zero')
        self.assertIsNone(zd.parse_twse({'stat': 'OK', 'date': '20260924', 'data': [['Total', '1', '1', '5']]}))

    def test_tw_dates_skip_weekend_known_and_today_before_close(self):
        now = datetime.datetime(2026, 9, 25, 10, 0)   # piątek 10:00 w Tajpej — dzisiejsza sesja jeszcze trwa
        d = zd.tw_dates({'2026-09-24'}, {'2026-09-22'}, now, first=False)
        self.assertNotIn('2026-09-25', d); self.assertNotIn('2026-09-24', d); self.assertNotIn('2026-09-22', d)
        self.assertNotIn('2026-09-20', d); self.assertIn('2026-09-23', d); self.assertEqual(d, sorted(d))
        self.assertIn('2026-09-25', zd.tw_dates(set(), set(), datetime.datetime(2026, 9, 25, 17, 0), first=False))

    def test_build_merges_history_converts_twd_and_keeps_failed_part(self):
        prev = {'in': {'d': [['2026-09-22', -67.67, -137.58, 2.82, -212.95, 95.8]]},
                'tw': {'d': [['2026-09-23', -1000.0, 0, 0, 0, -31.4, '2026-09-18']], 'empty': []}}

        def gj(url, headers=None, timeout=30):
            if 'DEXTAUS' in url:
                return {'observations': [{'date': '2026-09-18', 'value': '31.82'}, {'date': '2026-09-17', 'value': '.'}]}
            day = url.split('dayDate=')[1][:8]
            iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
            return self.tw(iso) if iso == '2026-09-24' else self.tw(iso, stat='x')

        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: self.NSDL.encode()), \
                mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd.time, 'sleep', lambda s: None), \
                mock.patch.object(zd, 'nsdl_http', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('bez sieci w teście'))), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.build_obce('KLUCZ', prev)
        self.assertEqual([r[0] for r in out['in']['d']], ['2026-09-22', '2026-09-23', '2026-09-24'], 'historia z poprzedniego pliku zostaje')
        tw = {r[0]: r for r in out['tw']['d']}
        self.assertEqual(tw['2026-09-24'][5:], [round(-32964.6 / 31.82, 1), '2026-09-18'])
        self.assertIn('2026-09-24', tw); self.assertIn('2026-09-23', tw)
        self.assertIn('2026-09-22', out['tw']['empty'], 'dzień bez sesji zapamiętany'); self.assertNotIn('2026-09-25', out['tw']['empty'], 'dzisiejszy brak nie jest świętem')
        self.assertEqual({k: v for k, v in zd.META['ok'].items() if k not in ('obce_hk', 'obce_br', 'obce_tr', 'obce_th')}, {'obce_in': True, 'obce_tw': True})
        self.assertFalse(any('KLUCZ' in e for e in zd.META['errors']), 'klucz nigdy w komunikatach')

        def boom(url, headers=None, timeout=60):
            raise RuntimeError('HTTP Error 503')
        zd.META['ok'].clear()
        with mock.patch.object(zd, 'get_bytes', boom), mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd.time, 'sleep', lambda s: None), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out2 = zd.build_obce('', prev)
        self.assertIs(out2['in'], prev['in'], 'awaria NSDL: poprzednia część zostaje')
        self.assertFalse(zd.META['ok']['obce_in']); self.assertTrue(any(e.startswith('NSDL:') for e in zd.META['errors']))


class EtfHistoryV55(unittest.TestCase):
    """v55: SoSoValue oddaje ok. 21 dni — suma 22 sesji z historii poprzedniego pliku, tylko gdy okna się nakładają."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    @staticmethod
    def days(start, n, v=1.0):
        d0 = datetime.date.fromisoformat(start); out = []
        while len(out) < n:
            if d0.weekday() < 5:
                out.append(d0.isoformat())
            d0 += datetime.timedelta(days=1)
        return out

    def test_merge_overlap_extends_and_new_values_win(self):
        old = [[zd.ts(d), 1.0] for d in self.days('2026-08-24', 21)]
        new = [[zd.ts(d), 2.0] for d in self.days('2026-08-25', 21)]
        m = zd.etf_merge_days(old, new)
        self.assertEqual(len(m), 22); self.assertEqual(m[0], old[0]); self.assertEqual(m[-1][1], 2.0)
        self.assertEqual(sum(1 for r in m if r[1] == 2.0), 21, 'nakładające się dni: wartość z nowego okna')

    def test_merge_without_overlap_keeps_only_new(self):
        old = [[zd.ts(d), 1.0] for d in self.days('2026-06-01', 21)]
        new = [[zd.ts(d), 2.0] for d in self.days('2026-08-25', 21)]
        self.assertEqual(zd.etf_merge_days(old, new), new, 'luka między oknami — bez sklejania')
        self.assertEqual(zd.etf_merge_days(None, new), new)
        self.assertEqual(zd.etf_merge_days([['x', 1], [zd.ts('2026-08-25'), 'a']], new), new, 'śmieci z poprzedniego pliku pominięte')

    def test_build_uses_previous_history_for_22_sessions(self):
        new_dates = self.days('2026-08-25', 21)
        prev = {'assets': {s: {'day': [[zd.ts(d), 10.0] for d in self.days('2026-08-24', 21)]} for s in zd.ETF_SYMS}}
        history = {s: _rows(new_dates) for s in zd.ETF_SYMS}
        with mock.patch.object(zd, 'soso', _soso_factory(history, {s: [] for s in zd.ETF_SYMS}, {})), \
                mock.patch.object(zd, 'get_json', side_effect=RuntimeError('brak sieci')):
            out = zd.build_etf('klucz', '', prev)
            out0 = zd.build_etf('klucz', '')
        a = out['assets']['btc']
        self.assertEqual((len(a['day']), a['m_n']), (22, 22)); self.assertAlmostEqual(a['m'], 10.0 + 21 * 100.0)
        self.assertIsNone(out0['assets']['btc']['m'], 'bez poprzedniego pliku: 21 dni = brak sumy 22 sesji (bez zmian)')


class EerV56(unittest.TestCase):
    """v56: kursy efektywne BIS — zmiana 30 dni z danych dziennych, 12 mies. ze średnich miesięcznych; brak = None."""
    D = ('FREQ,EER_TYPE,EER_BASKET,REF_AREA,TIME_PERIOD,OBS_VALUE\n'
         'D,N,B,JP,2026-08-21,68.00\nD,N,B,JP,2026-08-24,68.06\nD,N,B,JP,2026-09-22,69.16\n'
         'D,N,B,US,2026-09-01,101.0\nD,N,B,US,2026-09-22,102.05\n'
         'D,N,B,SA,2026-09-22,NaN\n')
    M = ('FREQ,EER_TYPE,EER_BASKET,REF_AREA,TIME_PERIOD,OBS_VALUE\n'
         'M,N,B,JP,2025-08,70.0\nM,N,B,JP,2026-07,68.5\nM,N,B,JP,2026-08,68.25\nM,N,B,US,2026-08,101.5\n')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear()

    def test_summary_30_days_and_12_months(self):
        rows = zd.eer_summary(zd.parse_cbpol_csv(self.D.encode()), zd.parse_cbpol_csv(self.M.encode()))
        jp, us = rows['JP'], rows['US']
        self.assertEqual((jp['v'], jp['d']), (69.16, '2026-09-22'))
        self.assertEqual(jp['c30'], round((69.16 / 68.00 - 1) * 100, 2), 'baza: ostatni dzień sprzed co najmniej 30 dni (21.08; 24.08 to tylko 29 dni)')
        self.assertEqual((jp['m'], jp['c12']), ('2026-08', round((68.25 / 70.0 - 1) * 100, 2)))
        self.assertIsNone(us['c30'], 'historia krótsza niż 30 dni = brak, nie zero'); self.assertIsNone(us['c12'], 'brak miesiąca rok wcześniej')
        self.assertNotIn('SA', rows, 'NaN pominięte')

    def test_build_urls_and_monthly_failure_is_soft(self):
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url)
            if '/M.N.B.' in url:
                raise RuntimeError('HTTP Error 500')
            return self.D.encode()
        with mock.patch.object(zd, 'get_bytes', gb):
            out = zd.build_eer()
        self.assertIn('D.N.B.US+XM', seen[0]); self.assertIn('detail=dataonly', seen[0])
        self.assertEqual(out['asof'], '2026-09-22'); self.assertIsNone(out['rows']['JP']['c12'])
        self.assertTrue(any(e.startswith('BIS kursy efektywne (miesięczne)') for e in zd.META['errors']))


class StablecoinChainsV58(unittest.TestCase):
    """v58: stablecoiny per sieć (DefiLlama /stablecoins) — tylko dolarowe; brak poprzedniej wartości = poza oknem, nie zero."""

    def test_sums_per_chain_and_missing_previous_is_left_out(self):
        j = {'peggedAssets': [
            {'pegType': 'peggedUSD', 'chainCirculating': {
                'Ethereum': {'current': {'peggedUSD': 100.0}, 'circulatingPrevDay': {'peggedUSD': 99.0}, 'circulatingPrevWeek': {'peggedUSD': 90.0}, 'circulatingPrevMonth': {'peggedUSD': 80.0}},
                'Solana': {'current': {'peggedUSD': 10.0}, 'circulatingPrevWeek': {'peggedUSD': 5.0}}}},
            {'pegType': 'peggedUSD', 'chainCirculating': {
                'Solana': {'current': {'peggedUSD': 30.0}, 'circulatingPrevDay': {'peggedUSD': 30.0}, 'circulatingPrevWeek': {'peggedUSD': 20.0}, 'circulatingPrevMonth': {'peggedUSD': 10.0}}}},
            {'pegType': 'peggedEUR', 'chainCirculating': {'Ethereum': {'current': {'peggedUSD': 999.0}}}}]}
        out = zd.parse_stabc(j)
        self.assertEqual(out['rows'][0], ['Ethereum', 100, 1, 10, 20], 'stablecoin euro pominięty')
        self.assertEqual(out['rows'][1], ['Solana', 40, 0, 15, 20], '30 dni: aktywo bez wartości sprzed miesiąca poza oknem (nie liczone jako 0)')
        self.assertEqual(out['total'], [140, 1, 25, 40]); self.assertEqual(out['n'], 2)
        with self.assertRaises(RuntimeError):
            zd.parse_stabc({'data': []})


class CoferV59(unittest.TestCase):
    """v59: MFW COFER — udziały walut w rezerwach świata (kwartalnie); zmiany tylko z dokładnego kwartału; brak = None."""
    DIMS = [('COUNTRY', ['G001']), ('INDICATOR', ['AFXRA', 'TFXRA', 'TFXRA_IMP']), ('FXR_CURRENCY', ['CI_EUR', 'CI_T', 'CI_USD']),
            ('TYPE_OF_TRANSFORMATION', ['NV_USD', 'SHRO_PT']), ('FREQUENCY', ['Q'])]
    PER = ['2025-Q2', '2024-Q2', '2025-Q1', 'zły']

    def test_quarters_shares_changes_and_missing(self):
        series = {'0:0:2:1:0': {0: '56.32', 1: '58.20', 2: '57.80'},        # USD udział
                  '0:0:2:0:0': {0: '7000000000000', 1: '6900000000000'},     # USD wartość
                  '0:0:0:1:0': {0: '20.10', 2: 'NaN'},                        # EUR udział; kwartał wcześniej NaN = brak
                  '0:0:1:0:0': {0: '12400000000000', 3: '1'},                 # suma przypisanych; okres „zły” pominięty
                  '0:1:1:0:0': {0: '13000000000000'},                          # suma wszystkich rezerw
                  '0:2:1:1:0': {0: '10.65'}}                                   # udział szacowany przez MFW
        out = zd.parse_cofer(_imf_sdmx(series, self.PER, self.DIMS))
        self.assertEqual(out['asof'], '2025-Q2'); self.assertEqual((out['alloc'], out['total'], out['alloc_pct'], out['imp_pct']), (12400.0, 13000.0, 95.4, 10.65))
        usd, eur = out['rows']['USD'], out['rows']['EUR']
        self.assertEqual((usd['sh'], usd['d1'], usd['d4'], usd['v'], usd['dv4']), (56.32, -1.48, -1.88, 7000.0, 100.0))
        self.assertEqual((eur['sh'], eur['d1'], eur['d4'], eur['v']), (20.1, None, None, None), 'brak = None, nie zero')
        self.assertEqual(out['order'], ['USD', 'EUR'])
        self.assertEqual(zd._q_add('2025-Q1', -1), '2024-Q4'); self.assertEqual(zd._q_add('2025-Q2', -4), '2024-Q2')
        with self.assertRaises(RuntimeError):
            zd.parse_cofer(_imf_sdmx({'0:0:2:1:0': {0: '56'}}, self.PER, self.DIMS))


class EtfHongKongV61(unittest.TestCase):
    """v61: ETF-y z Hongkongu — osobna część pliku; awaria = notatka, część USA bez zmian; historia jak w USA."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_hk_part_note_with_field_names_and_failure_is_only_a_note(self):
        dates = ['2026-09-22', '2026-09-23']
        seen = []

        def soso(path, key, _retry=True):
            seen.append(path)
            if 'country_code=HK' in path:
                if 'ETH' in path:
                    raise RuntimeError('HTTP Error 400')
                return [{'date': d, 'total_net_inflow': 5e6, 'cum_net_inflow': 3e8, 'total_net_assets': 4e8} for d in dates]
            if path.startswith('/etfs/summary-history'):
                return _rows(dates)
            return []
        with mock.patch.object(zd, 'soso', soso), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('brak sieci')):
            out = zd.build_etf('klucz', '')
        self.assertEqual(sorted(out['hk']), ['btc'], 'ETH z błędem — pominięty, BTC jest')
        b = out['hk']['btc']
        self.assertEqual((b['d1'], b['w'], b['m'], b['cum'], b['aum']), (5.0, None, None, 300.0, 400.0), 'za krótko na 5 i 22 sesje = brak')
        self.assertEqual(sorted(out['assets']), sorted(zd.ETF_SYMS), 'część USA bez zmian')
        self.assertTrue(any(n.startswith('SoSoValue HK BTC: 2 dni do 2026-09-23; pola: cum_net_inflow, date,') for n in zd.META['notes']))
        self.assertTrue(any(n.startswith('SoSoValue HK ETH: HTTP Error 400') for n in zd.META['notes']))
        self.assertFalse(any('HK' in e for e in zd.META['errors']), 'Hongkong nigdy jako błąd strony')
        self.assertTrue(any('country_code=HK' in p for p in seen))


class TdHistoryV64(unittest.TestCase):
    """v64: plik cen ma ponad rok sesji (kwartał i rok na mapie z cen ETF-ów); koszt zapytania bez zmian."""

    def test_output_size_covers_a_year(self):
        self.assertGreaterEqual(zd.TD_OUTPUT, 253, '1R = 252 sesje + punkt odniesienia')
        seen = []

        def get(url, headers=None, timeout=30):
            seen.append(url); return 200, json.dumps({'SPY': {'status': 'error', 'code': 400, 'message': 'x'}})
        with mock.patch.object(zd, 'get', get):
            zd.td_batch(['SPY'], 'KLUCZ', _retry=False)
        self.assertIn('outputsize=260', seen[0])


class HkexV67(unittest.TestCase):
    """v67: HKEX Stock Connect southbound — kupno − sprzedaż (SSE + SZSE), mln HKD; 404 w przeszłości = dzień bez sesji."""
    JS = ('tabData = [{"id":0,"date":"2026-09-24","market":"SSE Northbound","tradingDay":1,"content":[{"table":{"schema":[["Total Turnover","DQB"]],"tr":[{"td":[["113,943.72"]]},{"td":[["999"]]}]}}]},'
          '{"id":1,"date":"2026-09-24","market":"SSE Southbound","tradingDay":1,"content":[{"table":{"schema":[["Total Turnover","Buy Turnover","Sell Turnover"]],"tr":[{"td":[["41,143.90"]]},{"td":[["22,121.38"]]},{"td":[["19,022.52"]]}]}}]},'
          '{"id":3,"date":"2026-09-24","market":"SZSE Southbound","tradingDay":1,"content":[{"table":{"schema":[["Total Turnover","Buy Turnover","Sell Turnover"]],"tr":[{"td":[["21,856.55"]]},{"td":[["10,828.73"]]},{"td":[["11,027.82"]]}]}}]}];')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_parse_southbound_net_and_no_session(self):
        self.assertEqual(zd.parse_hkex(self.JS), ['2026-09-24', 2899.77, 32950.11, 30050.34, 2])
        self.assertIs(zd.parse_hkex(self.JS.replace('"tradingDay":1', '"tradingDay":0')), False, 'v69: dzień bez sesji (święto) = False, nie zero')
        self.assertIsNone(zd.parse_hkex('<html>404</html>'))

    def test_part_404_marks_holiday_and_converts_hkd(self):
        def gb(url, headers=None, timeout=60):
            d = url.split('daily_')[1][:8]
            if d == '20260924':
                return self.JS.encode()
            if d == '20260925':
                raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)   # dzisiejszy plik jeszcze nie wyszedł
            return self.JS.replace('"tradingDay":1', '"tradingDay":0').replace('2026-09-24', d[:4] + '-' + d[4:6] + '-' + d[6:]).encode()   # v69: święto = plik z tradingDay 0

        def gj(url, headers=None):
            return {'observations': [{'date': '2026-09-18', 'value': '7.8456'}]}
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd.time, 'sleep', lambda s: None), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.hkex_part(None, 'KLUCZ')
        self.assertEqual(out['d'][-1][:5], ['2026-09-24', 2899.77, 32950.11, 30050.34, 2])
        self.assertEqual(out['d'][-1][5:], [round(2899.77 / 7.8456, 1), '2026-09-18'])
        self.assertIn('2026-09-23', out['empty']); self.assertNotIn('2026-09-25', out['empty'], 'dzisiejszy brak nie jest świętem')
        self.assertFalse(zd.META['errors'])


class CftcExtraV68(unittest.TestCase):
    """v68: dodatkowe rynki CFTC (jen, funt, …) z tych samych plików; brak dodatkowego rynku = notatka, nie błąd."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear()

    def test_extra_market_parsed_and_missing_extra_is_only_a_note(self):
        wk = _CFTC_WK.replace(',099741,', ',097741,').replace('EURO FX - CHICAGO', 'JAPANESE YEN - CHICAGO')
        std = _cftc_std(); std[zd.CFTC_WEEK_URL] = (_CFTC_WK.rstrip('\r\n') + '\r\n' + wk).encode()
        out = zd.build_cftc(fetch=_cftc_fetch(std), today=_CFTC_TODAY)
        self.assertEqual(zd.META['errors'], [], 'brak pozostałych dodatkowych rynków to nie błąd')
        jpy = out['markets']['jpy']
        self.assertEqual((jpy['code'], jpy['asof']), ('097741', '2026-09-15')); self.assertIn('JAPANESE YEN', jpy['name'])
        self.assertEqual(jpy['groups']['lev_funds']['net'], out['markets']['eur']['groups']['lev_funds']['net'])
        self.assertTrue(any(n.startswith('CFTC gbp: brak rynku 096742') for n in zd.META['notes']))
        self.assertNotIn('gbp', {k for k, v in out['markets'].items() if v})


class CalendarAndHkexV69(unittest.TestCase):
    """v69: jeden kalendarz sesji dla ETF-ów; 404 HKEX za dzień roboczy z przeszłości = błąd do ponowienia, nie święto."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear()

    def test_fake_holiday_candle_removed(self):
        q = {'SPY': {'asof': '2025-12-26', 'd': [['2025-12-24', 1, 1], ['2025-12-26', 2, 1]]},
             'TUR': {'asof': '2025-12-26', 'd': [['2025-12-24', 1, 1], ['2025-12-25', 1, 1], ['2025-12-26', 2, 1]]}}
        self.assertEqual(zd._align_calendar(q), 1)
        self.assertEqual([r[0] for r in q['TUR']['d']], ['2025-12-24', '2025-12-26'])
        self.assertTrue(any('spoza wspólnego kalendarza' in n for n in zd.META['notes']))

    def test_hkex_past_404_is_a_failure_not_a_holiday(self):
        def gb(url, headers=None, timeout=60):
            d = url.split('daily_')[1][:8]
            if d == '20260924':
                return HkexV67.JS.encode()
            raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd.time, 'sleep', lambda s: None), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.hkex_part(None, '')
        self.assertNotIn('2026-09-23', out['empty'], 'brak pliku to nie święto')
        self.assertTrue(any(e.startswith('HKEX:') and 'HTTP 404' in e for e in zd.META['errors']))


class BilansV70(unittest.TestCase):
    """v70: MFW bilans płatniczy — mln USD, ostatni kwartał kraju, brak = brak (nie zero), straż skali, awaria = poprzedni plik."""
    DIMS = [('COUNTRY', ['KOR', 'USA', 'TWN']), ('BOP_ACCOUNTING_ENTRY', ['A_NFA_T', 'L_NIL_T', 'NETCD_T', 'CD_T']),
            ('INDICATOR', ['CAB', 'D_F', 'O_F', 'P_F', 'P_F5']), ('UNIT', ['USD']), ('FREQUENCY', ['Q'])]
    PER = ['2026-Q2', '2026-Q1', '2025-Q4', 'zły']

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_rows_units_quarters_and_missing(self):
        series = {'0:1:3:0:0': {0: '-47463300000', 1: '-41300000000', 3: '5'},   # KOR napływ portfelowy (okres „zły” pominięty)
                  '0:1:4:0:0': {0: '-63905400000'},                               # KOR akcje
                  '0:1:1:0:0': {1: '5961300000', 0: 'NaN'},                        # KOR bezpośrednie: 2026-Q2 NaN = brak
                  '0:0:3:0:0': {0: '18019000000'},                                # KOR mieszkańcy za granicę, portfel
                  '0:2:0:0:0': {0: '116630100000'},                               # KOR rachunek bieżący
                  '0:3:0:0:0': {0: '1'},                                           # CD_T nieużywany
                  '1:1:3:0:0': {1: '334100000000', 2: '423000000000'},             # USA napływ portfelowy
                  '2:1:3:0:0': {0: '1000000000'}}                                  # TWN spoza listy
        out = zd.parse_bilans(_imf_sdmx(series, self.PER, self.DIMS))
        kor, usa = out['rows']['KOR'], out['rows']['USA']
        self.assertEqual(kor['q'], '2026-Q2'); self.assertEqual(usa['q'], '2026-Q1')
        self.assertEqual(kor['s']['in_p'], [['2026-Q1', -41300.0], ['2026-Q2', -47463.3]])
        self.assertEqual(kor['s']['in_d'], [['2026-Q1', 5961.3]], 'NaN = brak, nie zero')
        self.assertEqual((kor['s']['in_pe'], kor['s']['out_p'], kor['s']['ca']), ([['2026-Q2', -63905.4]], [['2026-Q2', 18019.0]], [['2026-Q2', 116630.1]]))
        self.assertEqual(out['order'], ['USA', 'KOR']); self.assertEqual(out['asof_max'], '2026-Q2'); self.assertNotIn('TWN', out['rows'])
        with self.assertRaises(RuntimeError):
            zd.parse_bilans(_imf_sdmx({'1:1:3:0:0': {1: '334.1'}}, self.PER, self.DIMS))   # skala w mld zamiast USD
        with self.assertRaises(RuntimeError):
            zd.parse_bilans(_imf_sdmx({'0:2:0:0:0': {0: '1'}}, self.PER, self.DIMS))      # sam rachunek bieżący — bez napływu

    def test_build_url_and_main_flow_failure_keeps_previous(self):
        seen = []

        def gj(url, headers=None, timeout=60):
            seen.append(url); raise RuntimeError('HTTP Error 503')
        with mock.patch.object(zd, 'get_json', gj):
            with self.assertRaises(RuntimeError):
                zd.build_bilans()
        self.assertIn('IMF.STA/BOP/+/USA+CAN', seen[0]); self.assertIn('.L_NIL_T+A_NFA_T+NETCD_T.D_F+P_F+P_F5+P_F3+O_F+CAB.USD.Q?lastNObservations=8', seen[0])
        saved = {}
        prev = {'at': _iso(26 * 60), 'asof_max': '2026-Q1', 'rows': {}, 'order': []}
        offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                          'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in offs]
        try:
            with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                    mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), \
                    mock.patch.object(zd, 'previous', lambda name: prev if name == 'bilans' else None), \
                    mock.patch.object(zd, 'build_bilans', side_effect=RuntimeError('HTTP Error 503')):
                zd.main()
        finally:
            [p.stop() for p in offs]
        self.assertIs(saved['bilans'], prev); self.assertIs(zd.META['ok']['bilans'], False)
        self.assertIn('MFW bilans płatniczy: HTTP Error 503', zd.META['errors'])


class BcbV71(unittest.TestCase):
    """v71: BCB — dzienne przepływy dolarów przez rynek walutowy Brazylii; seria bez odpowiedzi = stare wartości / None, nie 0."""
    DAYS = [('17/09/2026', {13970: '-1346.26818222', 13968: '2685.49075138', 13969: '4031.75893360', 13967: '-329.92553221', 13961: '-1676.19371443'}),
            ('18/09/2026', {13970: '-330.40093998', 13968: '2955.74363009', 13969: '3286.14457007', 13967: '-62.09649291', 13961: '-392.49743289'})]

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def gj(self, broken=()):
        self.urls = []

        def f(url, headers=None):
            self.urls.append(url)
            sid = int(url.split('bcdata.sgs.')[1].split('/')[0])
            if sid in broken:
                raise RuntimeError('HTTP Error 503')
            if sid > 20000:   # v79: miesięczny bilans płatniczy
                return [{'data': '01/07/2026', 'valor': {22924: '300', 22927: '100', 22936: '100', 22939: '100'}.get(sid, '50')}]
            return [{'data': d, 'valor': v[sid]} for d, v in self.DAYS] + [{'data': 'zła', 'valor': '1'}, {'data': '19/09/2026', 'valor': 'NaN'}]
        return f

    def test_rows_identity_and_dates(self):
        with mock.patch.object(zd, 'get_json', self.gj()), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.bcb_part(None)
        self.assertEqual(out['d'], [['2026-09-17', -1346.27, 2685.49, 4031.76, -329.93, -1676.19], ['2026-09-18', -330.4, 2955.74, 3286.14, -62.1, -392.5]],
                         'zły zapis daty i NaN pominięte, nigdy 0')
        r = out['d'][-1]; self.assertAlmostEqual(r[1] + r[4], r[5], places=1)   # razem = finansowy + handlowy (tożsamość BCB)
        self.assertEqual(out['asof'], '2026-09-18'); self.assertEqual(zd.META['errors'], [])
        self.assertTrue(any('bcdata.sgs.13961/dados?formato=json&dataInicial=21/08/2025&dataFinal=25/09/2026' in u for u in self.urls), self.urls)

    def test_failed_series_keeps_old_column_and_reports(self):
        prev = {'d': [['2026-09-17', -1.0, 1.0, 2.0, 9.9, 8.9]]}
        with mock.patch.object(zd, 'get_json', self.gj(broken=(13967,))):
            out = zd.bcb_part(prev)
        rows = {r[0]: r for r in out['d']}
        self.assertEqual(rows['2026-09-17'][4], 9.9, 'seria bez odpowiedzi: stara wartość zostaje')
        self.assertIsNone(rows['2026-09-18'][4], 'nowy dzień bez tej serii = None, nie 0')
        self.assertEqual(rows['2026-09-18'][1], -330.4)
        self.assertTrue(any(e.startswith('BCB: 1 serie') for e in zd.META['errors']))
        with mock.patch.object(zd, 'get_json', self.gj(broken=(13970, 13968, 13969, 13967, 13961))):
            with self.assertRaises(RuntimeError):
                zd.bcb_part(prev)   # żadnego nowego dnia: build_obce zostawi poprzednią część


def _xlsx(sheets, shared=True):
    """Minimalny plik .xlsx do testów: {nazwa arkusza: [[komórki wiersza]]}; teksty jako wspólne napisy (t="s") albo inline."""
    import io, zipfile
    strs, buf = [], io.BytesIO()
    cref = lambda i: chr(65 + i)   # kolumny A–Z wystarczą w testach

    def cell(r, c, v):
        ref = f'{cref(c)}{r}'
        if isinstance(v, str):
            if shared:
                strs.append(v); return f'<c r="{ref}" t="s"><v>{len(strs) - 1}</v></c>'
            return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'
    with zipfile.ZipFile(buf, 'w') as z:
        wb, rels = [], []
        for n, (name, rows) in enumerate(sheets.items(), 1):
            wb.append(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>')
            rels.append(f'<Relationship Id="rId{n}" Type="x" Target="worksheets/sheet{n}.xml"/>')
            body = ''.join(f'<row r="{r}">' + ''.join(cell(r, c, v) for c, v in enumerate(row) if v is not None) + '</row>' for r, row in enumerate(rows, 1))
            z.writestr(f'xl/worksheets/sheet{n}.xml', f'<worksheet><sheetData>{body}</sheetData></worksheet>')
        z.writestr('xl/workbook.xml', '<workbook><sheets>' + ''.join(wb) + '</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships>' + ''.join(rels) + '</Relationships>')
        z.writestr('xl/sharedStrings.xml', '<sst>' + ''.join(f'<si><t>{s}</t></si>' for s in strs) + '</sst>')
    return buf.getvalue()


class SafeChinaVX(unittest.TestCase):
    """SAFE — kupno i sprzedaż walut przez banki w Chinach: wiersze po nazwie w sekcjach, mld USD, brak = None, straż skali."""
    HEAD = ['Item', 46204, 46235]   # 2026-07, 2026-08 (liczby seryjne Excela)
    ROWS = [['Monthly Data on Foreign Exchange Settlement and Sales by Banks (in USD)'], ['Unit: USD 100 million'], HEAD,
            ['I. Foreign exchange settlement', 2662.5672, 2518.2601], ['(I) by banks for themselves', 33.9, 35.8],
            ['(II) by banks for customers', 2628.6432, 2482.4808], ['1. Current Account', 2142.06, 2120.08],
            ['2. Capital and Financial Account', 486.5806, 362.3968], ['Including: Direct investment', 39.2, 33.2], ['       Portfolio investment', 434.0, 314.7],
            ['II. Foreign exchange sales', 2479.95, 2033.46], ['(I) by banks for themselves', 103.1, 70.2], ['(II) by banks for customers', 2376.8, 1963.2],
            ['1. Current Account', 1729.4, 1504.5], ['2. Capital and Financial Account', 647.3999, 458.7091], ['Including: Direct investment', 79.8, 44.8],
            ['       Portfolio investment', 520.6, 385.2],
            ['III. Balance', 182.6, 484.8], ['(I) by banks for themselves', -69.2, -34.4], ['(II) by banks for customers', 251.826, 519.2379],
            ['1. Current Account', 412.6453, 615.5502], ['   1.1 Trade in goods', 607.0, 746.1], ['2. Capital and Financial Account', -160.8193, -96.3123],
            ['Including: Direct investment', -40.5, -11.7], ['       Portfolio investment', -86.6, '-'],
            ['IV. Newly Signed Contract Amount of Forward Foreign Exchange Settlement and Sales', 420.4, 408.4], ['Balance', 1, 1]]

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_reader_parser_units_and_missing(self):
        for shared in (True, False):
            data = _xlsx({'in RMB (Monthly)': [['Item', 1]], 'in USD (Monthly)': self.ROWS}, shared=shared)
            m = zd.parse_safe(zd._xlsx_rows(data, 'in USD (Monthly)'))
            self.assertEqual(m, [['2026-07', 25.18, 41.26, -16.08, -4.05, -8.66, 48.66, 64.74],
                                 ['2026-08', 51.92, 61.56, -9.63, -1.17, None, 36.24, 45.87]], 'mld USD; „-” = brak, nie zero')
        self.assertEqual((zd._xlsx_month('2026.08'), zd._xlsx_month('46235'), zd._xlsx_month('Item')), ('2026-08', '2026-08', None))
        with self.assertRaises(RuntimeError):
            zd._xlsx_rows(_xlsx({'in RMB (Monthly)': [['Item', 1]]}), 'in USD (Monthly)')
        bad = [r if r[0] != '2. Capital and Financial Account' else [r[0], 4.866, 3.624] for r in self.ROWS]   # 100× za mało
        with self.assertRaises(RuntimeError):
            zd.parse_safe(zd._xlsx_rows(_xlsx({'in USD (Monthly)': bad}), 'in USD (Monthly)'))

    def test_build_finds_monthly_link_and_notes_identity(self):
        page = ('<a href="/en/file/file/20260915/aaa.xlsx">Data on Foreign Exchange Settlement and Sales by Banks in 2026 (by Region)</a>'
                '<a href="/en/file/file/20260915/bbb.xlsx"><span>Time-series Data of Foreign Exchange Settlement and Sales by Banks</span></a>')
        rows = [r if r[0] != '(II) by banks for customers' or r[1] != 251.826 else [r[0], 999.0, 519.2379] for r in self.ROWS]
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url)
            return page.encode() if url == zd.SAFE_PAGE else _xlsx({'in USD (Monthly)': rows})
        with mock.patch.object(zd, 'get_bytes', gb):
            out = zd.build_safe()
        self.assertEqual(seen[1], 'https://www.safe.gov.cn/en/file/file/20260915/bbb.xlsx', 'szereg czasowy, nie plik roczny')
        self.assertEqual((out['asof'], out['m'][-1][3]), ('2026-08', -9.63))
        self.assertTrue(any(n.startswith('SAFE: saldo klientów') and '2026-07' in n for n in zd.META['notes']), zd.META['notes'])
        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: b'<html>bez linku</html>'):
            with self.assertRaises(RuntimeError):
                zd.build_safe()


class ReviewV73(unittest.TestCase):
    """v73: święto w Chinach przy otwartym Hongkongu = brak sesji (nie zero); stare serie poza plikiem MFW; kalendarz ETF odporny."""

    @staticmethod
    def hk_file(day, buy='0.00', sell='0.00', trading=1, broken=False):
        mk = lambda name: {'id': 0, 'date': day, 'market': name, 'tradingDay': trading, 'content': [{'style': 1, 'table': {
            'schema': [['Total Turnover', 'Buy Turnover', 'Sell Turnover', 'Total Trade Count']],
            'tr': [] if broken else [{'td': [[str(float(buy) + float(sell))]]}, {'td': [[buy]]}, {'td': [[sell]]}, {'td': [['0']]}]}}]}
        north = {'id': 0, 'date': day, 'market': 'SSE Northbound', 'tradingDay': 0, 'content': []}
        return 'tabData = ' + json.dumps([north, mk('SSE Southbound'), mk('SZSE Southbound')]) + ';'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_mainland_holiday_is_no_session_and_old_zero_rows_move_to_empty(self):
        self.assertIs(zd.parse_hkex(self.hk_file('2026-05-05')), False, 'tradingDay 1 z obrotem 0,00 = brak sesji, nie zero')
        self.assertEqual(zd.parse_hkex(self.hk_file('2026-09-24', '100.5', '50.25')), ['2026-09-24', 100.5, 201.0, 100.5, 2])
        self.assertIsNone(zd.parse_hkex(self.hk_file('2026-09-24', broken=True)), 'nieczytelna tabela w dniu sesji = nieczytelny plik')
        prev = {'d': [['2026-09-23', 3448.93, 42794.07, 39345.14, 2, 439.6, '2026-09-18'], ['2026-09-22', 0.0, 0.0, 0.0, 2, 0.0, '2026-09-18']], 'empty': []}

        def gb(url, headers=None, timeout=60):
            d = url.split('daily_')[1][:8]
            return self.hk_file(f'{d[:4]}-{d[4:6]}-{d[6:]}').encode()   # każdy dzień: Chiny zamknięte
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd.time, 'sleep', lambda s: None), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.hkex_part(prev, '')
        self.assertEqual([r[0] for r in out['d']], ['2026-09-23'], 'dawny wiersz z zerem usunięty')
        self.assertIn('2026-09-22', out['empty']); self.assertIn('2026-09-24', out['empty'])

    def test_bilans_drops_series_that_ended_long_ago(self):
        dims = [('COUNTRY', ['VNM']), ('BOP_ACCOUNTING_ENTRY', ['L_NIL_T']), ('INDICATOR', ['D_F', 'P_F5']), ('UNIT', ['USD']), ('FREQUENCY', ['Q'])]
        out = zd.parse_bilans(_imf_sdmx({'0:0:0:0:0': {0: '5000000000'}, '0:0:1:0:0': {1: '100000000'}}, ['2026-Q1', '2014-Q4'], dims))
        self.assertEqual(out['rows']['VNM']['q'], '2026-Q1'); self.assertNotIn('in_pe', out['rows']['VNM']['s'], 'seria z 2014 poza plikiem')

    def test_calendar_uses_majority_when_spy_is_short_and_drops_empty_symbols(self):
        days = [f'2026-09-{d:02d}' for d in range(1, 21)]
        q = {'SPY': {'d': [[days[-1], 1, 1]]}, 'EWA': {'d': [[d, 1, 1] for d in days]}, 'EWJ': {'d': [[d, 1, 1] for d in days]},
             'TUR': {'d': [['2025-12-25', 1, 1]]}}
        zd._align_calendar(q)
        self.assertEqual(len(q['EWA']['d']), 20, 'krótka historia SPY nie obcina innych')
        self.assertNotIn('TUR', q); self.assertTrue(any('bez świec' in n and 'TUR' in n for n in zd.META['notes']))


class TurcjaV74(unittest.TestCase):
    """v74: CBRT — tygodniowe transakcje nierezydentów; tylko część B; nowszy plik poprawia tydzień; brak = None, nie 0."""
    ROWS = [[None, 'Table - 1. Shares and Debt Securities Held by Non-Residents (Million USD) (*)'],
            [None, 'A. STOCK (Market Value)', '18.09.2026', 46276], [None, 'STOCK TOTAL (**)', 148753.16, 153388.46], [None, 'Equity', 39355.01, 42410.94],
            [None, 'B. NET TRANSACTIONS (Adjusted for Market Prices and Exchange Rates)', '18.09.2026', 46276],
            [None, 'NET TRANSACTIONS TOTAL (**)', -316.01, 393.81], [None, 'B.1. Domestic Market Total (**)', -558.42, 423.06],
            [None, 'Equity', -109.83, 277.67], [None, 'GDDS (Outright Purchase)', -116.9, 151.15], [None, 'GDDS (Reverse Repo)', 0.61, 3.86],
            [None, 'Debt Securities Issued by Other Than General Government (***)', -331.69, '-'], [None, 'B.2. International Market Total', 242.41, -29.25],
            [None, 'General Government Issuances', -168.69, -52.28], [None, ''], [None, '(*) Data is disseminated provisionally']]

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def zipped(self, rows):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('Securities Statistics.xlsx', _xlsx({'Contents': [['x']], 'T1_En': rows}))
        return buf.getvalue()

    def test_part_b_only_rows_by_label_and_revision(self):
        d = zd.parse_tcmb(zd._xlsx_rows(_xlsx({'T1_En': self.ROWS}), 'T1_En'))
        self.assertEqual(d, [['2026-09-11', 393.81, 277.67, 151.15, None, -29.25, -52.28],
                             ['2026-09-18', -316.01, -109.83, -116.9, -331.69, 242.41, -168.69]], 'akcje z części B, nie ze stanu; „-” = brak')
        r = d[-1]; self.assertAlmostEqual(r[2] + r[3] + r[4] + r[5], r[1], places=2)
        page = ('<a href="/wps/wcm/connect/dc9e/Securities+Statistics.zip?MOD=AJPERES&amp;CACHEID=ROOT-q3">ZIP</a>')
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url)
            return page.encode() if url == zd.TCMB_PAGE else self.zipped(self.ROWS)
        prev = {'d': [['2026-09-04', 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], ['2026-09-11', 9.0, 9.0, 9.0, 9.0, 9.0, 9.0]]}
        with mock.patch.object(zd, 'get_bytes', gb):
            out = zd.tcmb_part(prev)
        self.assertEqual(seen[1], 'https://www.tcmb.gov.tr/wps/wcm/connect/dc9e/Securities+Statistics.zip?MOD=AJPERES&CACHEID=ROOT-q3')
        self.assertEqual([r[0] for r in out['d']], ['2026-09-04', '2026-09-11', '2026-09-18'], 'historia z poprzedniego pliku zostaje')
        self.assertEqual(out['d'][1][1], 393.81, 'nowszy plik poprawia tydzień'); self.assertEqual(out['asof'], '2026-09-18')
        with self.assertRaises(RuntimeError):
            zd.parse_tcmb(zd._xlsx_rows(_xlsx({'T1_En': self.ROWS[:4]}), 'T1_En'))


class EurostatUE(unittest.TestCase):
    """Eurostat bop_c6_m — JSON-stat → mln EUR, ostatni miesiąc kraju, brak = brak (nie zero), straż skali."""

    @staticmethod
    def js(values, geo=('DE', 'PL'), time=('2026-06', '2026-07')):
        dims = [('bop_item', ['FA__D__F', 'FA__P__F']), ('stk_flow', ['ASS', 'LIAB']), ('geo', list(geo)), ('time', list(time))]
        return {'id': [d for d, _ in dims], 'size': [len(v) for _, v in dims],
                'dimension': {d: {'category': {'index': {k: i for i, k in enumerate(v)}}} for d, v in dims}, 'value': values}

    def test_jsonstat_units_last_month_and_missing(self):
        # indeks = ((item*2 + flow)*2 + geo)*2 + time
        vals = {str(((1 * 2 + 1) * 2 + 0) * 2 + 1): 42440.0, str(((1 * 2 + 1) * 2 + 0) * 2 + 0): 27859.0,   # DE portfelowe napływ
                str(((1 * 2 + 1) * 2 + 1) * 2 + 1): 2422.2, str(((0 * 2 + 1) * 2 + 1) * 2 + 0): -935.4,      # PL portfelowe 07, bezpośrednie 06
                str(((1 * 2 + 0) * 2 + 1) * 2 + 1): 653.3}                                                    # PL portfelowe aktywa 07
        out = zd.parse_ue(self.js(vals))
        self.assertEqual(out['order'], ['DE', 'PL']); self.assertEqual(out['asof_max'], '2026-07')
        pl = out['rows']['PL']
        self.assertEqual(pl['m'], '2026-07'); self.assertEqual(pl['s']['in_p'], [['2026-07', 2422.2]]); self.assertEqual(pl['s']['in_d'], [['2026-06', -935.4]])
        self.assertEqual(pl['s']['out_p'], [['2026-07', 653.3]]); self.assertNotIn('in_o', pl['s'], 'brak = brak klucza, nie zero')
        self.assertEqual(out['rows']['DE']['s']['in_p'], [['2026-06', 27859.0], ['2026-07', 42440.0]])
        with self.assertRaises(RuntimeError):
            zd.parse_ue(self.js({str(((1 * 2 + 1) * 2 + 0) * 2 + 1): 42.44}))   # skala: mld zamiast mln
        with self.assertRaises(RuntimeError):
            zd.parse_ue(self.js({}))
        self.assertIn('bop_item=FA__P__F&bop_item=FA__D__F&bop_item=FA__O__F&stk_flow=LIAB&stk_flow=ASS', zd.UE_URL)
        self.assertIn('geo=PL', zd.UE_URL); self.assertIn('lastTimePeriod=24', zd.UE_URL)


class ReviewV77(unittest.TestCase):
    """v77: czytnik .xlsx odporny na puste komórki i wiersze, tekst sformatowany, komórki bez adresu, system dat 1904;
    CBRT — straż skali; obce.json pamięta stan i błędy części, widoczne także przy przebiegu z pamięci."""
    NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def book(self, sheet, wbpr=''):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('xl/workbook.xml', f'<workbook {self.NS}>{wbpr}<sheets><sheet name="A &amp; B" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                      '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/sharedStrings.xml', f'<sst {self.NS}><si><t>Item</t></si><si/><si><r><t>Rich</t></r><r><t xml:space="preserve"> text</t></r></si></sst>')
            z.writestr('xl/worksheets/sheet1.xml', f'<worksheet {self.NS}><sheetData>{sheet}</sheetData></worksheet>')
        return buf.getvalue()

    def test_reader_empty_cells_rows_rich_text_and_1904(self):
        sheet = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" s="2"/><c r="C1"><v>46235</v></c></row><row r="2"/>'
                 '<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3" t="s"><v>1</v></c><c t="inlineStr"><is><t>x&amp;y</t></is></c></row>')
        rows = zd._xlsx_rows(self.book(sheet), 'A & B')
        self.assertEqual(rows, {1: {1: 'Item', 3: '46235'}, 3: {1: 'Rich text', 2: '', 3: 'x&y'}}, 'pusta komórka nie przesuwa lipca/sierpnia')
        with self.assertRaises(RuntimeError):
            zd._xlsx_rows(self.book(sheet, '<workbookPr date1904="1"/>'), 'A & B')

    def test_cbrt_scale_guard(self):
        rows = [r if r[1] != 'NET TRANSACTIONS TOTAL (**)' else [None, r[1], -316010000.0, 393.81] for r in TurcjaV74.ROWS]
        page = '<a href="/x/Securities+Statistics.zip?MOD=AJPERES">ZIP</a>'
        z = TurcjaV74.zipped(None, rows)
        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: page.encode() if url == zd.TCMB_PAGE else z):
            with self.assertRaises(RuntimeError):
                zd.tcmb_part(None)

    def test_obce_part_status_kept_and_shown_on_cached_runs(self):
        def boom(prev, key=''):
            raise RuntimeError('HTTP Error 503')

        def hk(prev, key):
            zd.META['errors'].append('HKEX: 1 dni bez odpowiedzi, np. 2026-09-23: HTTP 404'); return {'d': [['2026-09-24', 1.0]]}
        with mock.patch.object(zd, 'nsdl_part', lambda prev: {'d': [['2026-09-24', 1.0]]}), mock.patch.object(zd, 'twse_part', boom), \
                mock.patch.object(zd, 'hkex_part', hk), mock.patch.object(zd, 'bcb_part', lambda prev: {'d': [['2026-09-18', 1.0]]}), \
                mock.patch.object(zd, 'tcmb_part', lambda prev: {'d': [['2026-09-18', 1.0]]}), \
                mock.patch.object(zd, 'thbma_part', lambda prev, key: {'d': [['2026-09-24', 1.0]]}):
            out = zd.build_obce('', {})
        self.assertEqual(out['ok'], {'in': True, 'tw': False, 'hk': True, 'br': True, 'tr': True, 'th': True})
        self.assertEqual(out['errs']['tw'], ['TWSE: HTTP Error 503']); self.assertTrue(out['errs']['hk'][0].startswith('HKEX: 1 dni'))
        zd.META['errors'].clear(); zd.META['ok'].clear(); saved = {}
        prev = dict(out, at=_iso(30))   # v80: część z błędem ponawiana po 60 min
        offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                          'build_kursy', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in offs]
        try:
            with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                    mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), \
                    mock.patch.object(zd, 'previous', lambda name: prev if name == 'obce' else None), \
                    mock.patch.object(zd, 'build_obce', side_effect=AssertionError('bez zapytań')):
                zd.main()
        finally:
            [p.stop() for p in offs]
        self.assertIs(saved['obce'], prev); self.assertEqual(zd.META['ok']['obce'], 'cached')
        self.assertIs(zd.META['ok']['obce_tw'], False); self.assertEqual(zd.META['ok']['obce_hk'], 'cached')
        self.assertIn('TWSE: HTTP Error 503', zd.META['errors']); self.assertTrue(any(e.startswith('HKEX: 1 dni') for e in zd.META['errors']))


class KanadaV78(unittest.TestCase):
    """v78: Statistics Canada — mln CAD, miesiąc z refPer, brak = None, skala ≠ miliony = błąd, notatka przy niezgodnej sumie."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    @staticmethod
    def vec(vid, pts, scale=6):
        return {'status': 'SUCCESS', 'object': {'vectorId': vid, 'vectorDataPoint': [
            {'refPer': d, 'value': v, 'scalarFactorCode': scale, 'statusCode': 0} for d, v in pts]}}

    def test_rows_units_identity_and_scale(self):
        j = [self.vec(61915649, [('2026-06-01', 41249.0), ('2026-07-01', 20653.0)]), self.vec(61915652, [('2026-07-01', 13453.0)]),
             self.vec(61915712, [('2026-07-01', 7200.0), ('2026-06-01', None)]), self.vec(61915682, [('2026-07-01', 25321.0)]),
             self.vec(61915655, [('2026-07-01', -11869.0), ('zły', 1)]), self.vec(999, [('2026-07-01', 5.0)])]
        m = zd.parse_kanada(j)
        self.assertEqual(m, [['2026-06', 41249.0, None, None, None, None], ['2026-07', 20653.0, 13453.0, 25321.0, -11869.0, 7200.0]], 'brak = None, nie zero')
        self.assertEqual(zd.META['notes'], [])
        j[1] = self.vec(61915652, [('2026-07-01', 1000.0)])
        zd.parse_kanada(j); self.assertTrue(any('razem ≠ dłużne + akcje' in n and '2026-07' in n for n in zd.META['notes']))
        with self.assertRaises(RuntimeError):
            zd.parse_kanada([self.vec(61915649, [('2026-07-01', 20.653)], scale=9)])
        with self.assertRaises(RuntimeError):
            zd.parse_kanada([])

    def test_build_url_one_get_with_five_vectors(self):
        seen = []
        with mock.patch.object(zd, 'get_json', lambda url, headers=None: seen.append(url) or [self.vec(61915649, [('2026-07-01', 1.0)])]), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.build_kanada()
        self.assertEqual(len(seen), 1); self.assertIn('vectorIds=%2261915649%22,%2261915652%22', seen[0])
        self.assertIn('startRefPeriod=2024-09-01&endReferencePeriod=2026-09-25', seen[0]); self.assertEqual(out['asof'], '2026-07')


class BrazyliaBopV79(unittest.TestCase):
    """v79: BCB miesięczny bilans płatniczy — mln USD, miesiąc z daty, seria bez odpowiedzi = stare wartości / None, awaria nie psuje dziennych."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_monthly_rows_identity_and_failure_isolated(self):
        vals = {22885: '7460.5', 22924: '2158.3', 22927: '1688.5', 22936: '167.9', 22939: '301.9', 22971: '3740.6', 22986: '11.7', 23001: '0', 23042: '0'}

        def gj(url, headers=None):
            sid = int(url.split('bcdata.sgs.')[1].split('/')[0])
            if sid == 22971:
                raise RuntimeError('HTTP Error 503')
            return [{'data': '01/06/2026', 'valor': '1'}, {'data': '01/07/2026', 'valor': vals[sid]}]
        with mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            rows = zd.bcb_bop([['2026-07', 1.0, 1.0, 1.0, 1.0, 1.0, 9.9]])
        self.assertEqual(rows[-1], ['2026-07', 7460.5, 2158.3, 1688.5, 167.9, 301.9, 9.9, 11.7, 0.0, 0.0], 'seria bez odpowiedzi: stara wartość zostaje')
        self.assertEqual(rows[0], ['2026-06', 1.0, 1.0, 1.0, 1.0, 1.0, None, 1.0, 1.0, 1.0], 'nowy miesiąc bez tej serii = None, nie 0')
        self.assertTrue(any(e.startswith('BCB bilans płatniczy: 1 serie') for e in zd.META['errors']))
        self.assertTrue(any('portfelowe ≠' in n and '2026-06' in n for n in zd.META['notes']), 'czerwiec 1 ≠ 1+1+1')

        def gj2(url, headers=None):
            sid = int(url.split('bcdata.sgs.')[1].split('/')[0])
            if sid > 20000:
                raise RuntimeError('HTTP Error 503')
            return [{'data': '18/09/2026', 'valor': '1'}]
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', gj2), mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.bcb_part({'m': [['2026-06', 1.0, 2.0, 1.0, 0.5, 0.5, 3.0]]})
        self.assertEqual(out['d'][-1][0], '2026-09-18'); self.assertEqual(out['m'], [['2026-06', 1.0, 2.0, 1.0, 0.5, 0.5, 3.0]], 'awaria miesięcznych — poprzednie wiersze zostają')
        self.assertTrue(any(e.startswith('BCB bilans płatniczy:') for e in zd.META['errors']))


class ReviewV80(unittest.TestCase):
    """v80: Eurostat — pozostałe bez banku centralnego, miesiąc z kompletem, statusy, 24 miesiące; obce — brak części = pobierz;
    czytnik xlsx bez <rPh>; JSON-stat z indeksem jako lista."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    @staticmethod
    def js(values, status=None, geo=('DE', 'PL'), time=('2026-06', '2026-07'), as_list=False):
        dims = [('bop_item', ['FA__D__F', 'FA__O__F', 'FA__P__F']), ('sector10', ['S1', 'S121']), ('stk_flow', ['ASS', 'LIAB']), ('geo', list(geo)), ('time', list(time))]
        cat = lambda v: v if as_list else {k: i for i, k in enumerate(v)}
        return {'id': [d for d, _ in dims], 'size': [len(v) for _, v in dims], 'dimension': {d: {'category': {'index': cat(v)}} for d, v in dims},
                'value': values, 'status': status or {}}

    @staticmethod
    def ix(item, sec, flow, geo, time):
        return str((((item * 2 + sec) * 2 + flow) * 2 + geo) * 2 + time)

    def test_other_without_central_bank_full_month_and_flags(self):
        ix = self.ix
        v = {ix(2, 0, 1, 1, 1): 2422.2, ix(0, 0, 1, 1, 1): 509.5, ix(1, 0, 1, 1, 1): 7517.0, ix(1, 1, 1, 1, 1): 6834.0,   # PL 07: portfelowe, bezpośrednie, pozostałe S1 i S121
             ix(2, 0, 1, 1, 0): 100.0, ix(0, 0, 1, 1, 0): 50.0, ix(1, 0, 1, 1, 0): 999.0,                                   # PL 06: pozostałe bez S121
             ix(2, 0, 1, 0, 1): 42440.0, ix(2, 0, 1, 0, 0): 50.0}                                                          # DE: miesiąc bliski zera jest dozwolony
        out = zd.parse_ue(self.js(v, status={ix(2, 0, 1, 1, 1): 'e', ix(1, 1, 1, 1, 1): '|C'}))
        pl = out['rows']['PL']
        self.assertEqual(pl['s']['in_o'], [['2026-07', 683.0]], 'pozostałe bez banku centralnego; czerwiec bez S121 = brak, nie suma z TARGET2')
        self.assertEqual(pl['m'], '2026-07'); self.assertEqual(pl['f'], {'2026-07': 'e'})
        self.assertEqual(out['rows']['DE']['m'], '2026-07', 'bez kompletu — ostatni miesiąc z czymkolwiek')
        out2 = zd.parse_ue(self.js(v, as_list=True)); self.assertEqual(out2['rows']['PL']['s']['in_o'], [['2026-07', 683.0]], 'indeks kategorii jako lista')
        self.assertIn('sector10=S1&sector10=S121', zd.UE_URL); self.assertIn('lastTimePeriod=24', zd.UE_URL)
        with self.assertRaises(RuntimeError):
            zd.parse_ue(self.js({ix(2, 0, 1, 0, 1): 42.4, ix(2, 0, 1, 0, 0): 30.0}))   # skala: całe Niemcy poniżej 100 mln

    def test_latest_complete_month_wins(self):
        ix = self.ix
        v = {ix(2, 0, 1, 1, 0): 1.0, ix(0, 0, 1, 1, 0): 1.0, ix(1, 0, 1, 1, 0): 3.0, ix(1, 1, 1, 1, 0): 1.0, ix(2, 0, 1, 1, 1): 5.0,
             ix(2, 0, 1, 0, 1): 42440.0}
        self.assertEqual(zd.parse_ue(self.js(v))['rows']['PL']['m'], '2026-06', 'lipiec bez kompletu — czerwiec z kompletem')

    def test_obce_missing_part_forces_rebuild(self):
        prev = {'at': _iso(30), 'in': {'d': [['2026-09-24', 1.0]]}, 'tw': {'d': []}, 'hk': {'d': []}}
        new = {'at': zd.NOW, 'in': {}, 'tw': {}, 'hk': {}, 'br': {}, 'tr': {}}
        saved = {}
        offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                          'build_kursy', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in offs]
        try:
            with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                    mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), \
                    mock.patch.object(zd, 'previous', lambda name: prev if name == 'obce' else None), \
                    mock.patch.object(zd, 'build_obce', return_value=new):
                zd.main()
        finally:
            [p.stop() for p in offs]
        self.assertIs(saved['obce'], new, 'brakuje części br i tr — pobieramy od nowa, choć plik jest świeży'); self.assertIs(zd.META['ok']['obce'], True)

    def test_xlsx_phonetic_runs_skipped(self):
        book = ReviewV77.book(ReviewV77(), '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')
        import io, zipfile
        src = zipfile.ZipFile(io.BytesIO(book)); buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for n in src.namelist():
                data = src.read(n)
                if n == 'xl/sharedStrings.xml':
                    data = data.replace(b'<si><t>Item</t></si>', b'<si><r><t>Kanji</t></r><rPh sb="0" eb="1"><t>kana</t></rPh></si>')
                z.writestr(n, data)
        self.assertEqual(zd._xlsx_rows(buf.getvalue(), 'A & B'), {1: {1: 'Kanji'}})


class UeFormatV80(unittest.TestCase):
    """v80: ue.json sprzed v80 (pozostałe z bankiem centralnym) jest przebudowywany mimo świeżości."""

    def test_old_format_rebuilt_new_format_cached(self):
        for unit, rebuilt in (('mln EUR, transakcje w miesiącu', True), ('… o pozostałe bez banku centralnego (S1 − S121)', False)):
            zd.META['errors'].clear(); zd.META['ok'].clear(); saved = {}
            prev = {'at': _iso(30), 'unit': unit, 'rows': {}, 'order': []}
            new = {'at': zd.NOW, 'unit': 'S121', 'rows': {}, 'order': []}
            offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                    for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                              'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
            [p.start() for p in offs]
            try:
                with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                        mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), \
                        mock.patch.object(zd, 'previous', lambda name: prev if name == 'ue' else None), \
                        mock.patch.object(zd, 'build_ue', return_value=new):
                    zd.main()
            finally:
                [p.stop() for p in offs]
            self.assertIs(saved['ue'], new if rebuilt else prev, unit)


class KoreaV82(unittest.TestCase):
    """v82: FSS — tylko stałe zdanie komunikatu; bln/mld KRW; kupno +, sprzedaż −; historia z listy; kurs Fed; brak = None."""
    LIST = ('<a href="/eng/bbs/B0000211/view.do?nttId=229240&amp;menuNo=400010">Foreign Investors&#39; Stock and Bond Investment, August 2026</a>'
            '<a href="/eng/bbs/B0000211/view.do?nttId=1">Delinquency Rate on Domestic Banks</a>'
            '<a href="/eng/bbs/B0000211/view.do?nttId=223993&amp;menuNo=400010">Foreign Investors\' Stock and Bond Investment, July 2026</a>')
    AUG = '<div><p>Foreign investors bought a net KRW344.0 billion of listed stocks and sold a net KRW4.7360 trillion of listed bonds in August 2026. Foreign ...</p></div>'
    JUL = '<p>Foreign investors sold a net KRW31.6640 trillion of listed stocks and bought a net KRW2,388.0 billion of listed bonds in July 2026.</p>'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_sentence_units_and_signs(self):
        self.assertEqual(zd.parse_fss(self.AUG), ['2026-08', 344.0, -4736.0])
        self.assertEqual(zd.parse_fss(self.JUL), ['2026-07', -31664.0, 2388.0])
        self.assertIsNone(zd.parse_fss('<p>Foreign investors were active in August 2026.</p>'), 'inne zdanie = brak, nie zgadywanie')
        self.assertEqual(zd.parse_fss('Foreign investors bought a net KRW1.5240 trillion of listed stocks and a net KRW7.8870 trillion of listed bonds in December 2025.'),
                         ['2025-12', 1524.0, 7887.0], 'bez drugiego czasownika — ten sam kierunek (komunikat z 12.2025)')
        self.assertEqual(zd.parse_fss('Foreign investors sold a net KRW13.3730 trillion of listed stock and bought a net KRW16.2540 trillion of listed bonds in November 2025.'),
                         ['2025-11', -13373.0, 16254.0], '„stock” w liczbie pojedynczej (komunikat z 11.2025)')

    def test_build_history_rate_and_unreadable_release(self):
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url)
            if 'list.do' in url:
                return self.LIST.encode() if url.endswith('pageIndex=1') else b'<html></html>'
            return (self.AUG if '229240' in url else '<p>Zmieniony układ</p>').encode()
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, 'fred_rates', lambda key, sid: {'2026-08-01': 1380.0}), \
                mock.patch.object(zd, '_now_utc', lambda: datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
            out = zd.build_korea('KLUCZ', {'m': [['2026-06', -49336.0, 4478.0, None, None, None]]})
        m = {r[0]: r for r in out['m']}
        self.assertEqual(m['2026-08'], ['2026-08', 344.0, -4736.0, round(344000 / 1380, 1), round(-4736000 / 1380, 1), 1380.0])
        self.assertEqual(m['2026-06'][:3], ['2026-06', -49336.0, 4478.0], 'historia z poprzedniego pliku zostaje')
        self.assertNotIn('2026-07', m, 'nieczytelny komunikat — bez miesiąca, nie zero')
        self.assertTrue(any(e.startswith('FSS: 1 komunikaty nieczytelne') for e in zd.META['errors']))
        self.assertIn('https://www.fss.or.kr/eng/bbs/B0000211/view.do?nttId=229240&menuNo=400010', seen)
        self.assertEqual(sum('list.do' in u for u in seen), zd.FSS_PAGES, 'v83: brakujący lipiec — szukamy na kolejnych stronach')
        self.assertIn('FSS: brak komunikatów za miesiące: 2026-07', zd.META['errors'], 'brak zgłaszany przy każdym przebiegu')
        self.assertFalse(any('KLUCZ' in e for e in zd.META['errors']))


class KoreaV83(unittest.TestCase):
    """v83: FSS — komplet miesięcy = jedna strona listy; strona 1 czytana zawsze (poprawki); brak nowego komunikatu = błąd; limit wartości."""

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def run_(self, pages, views, prev, now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url)
            if 'list.do' in url:
                return pages.get(int(url.rsplit('=', 1)[1]), '<html></html>').encode()
            return views[url.split('nttId=')[1].split('&')[0]].encode()
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, '_now_utc', lambda: now):
            return zd.build_korea('', prev), seen

    def test_complete_one_list_page_and_correction(self):
        page1 = KoreaV82.LIST
        views = {'229240': KoreaV82.AUG, '223993': KoreaV82.JUL.replace('KRW31.6640 trillion', 'KRW31.7000 trillion')}
        out, seen = self.run_({1: page1}, views, {'m': [['2026-07', -31664.0, 2388.0, None, None, None]]})
        self.assertEqual(sum('list.do' in u for u in seen), 1, 'komplet miesięcy — tylko strona 1')
        self.assertEqual({r[0]: r[1] for r in out['m']}['2026-07'], -31700.0, 'strona 1 czytana ponownie — poprawka wchodzi')
        self.assertEqual(zd.META['errors'], [])

    def test_stale_and_sanity(self):
        out, _ = self.run_({}, {}, {'m': [['2026-05', 1.0, 1.0, None, None, None]]})
        self.assertTrue(any(e.startswith('FSS: brak nowego komunikatu po 2026-05') for e in zd.META['errors']), zd.META['errors'])
        zd.META['errors'].clear()
        big = KoreaV82.AUG.replace('KRW344.0 billion', 'KRW4,7360 trillion')   # przecinek jako separator dziesiętny = 47 360 000 mld
        out, _ = self.run_({1: KoreaV82.LIST}, {'229240': big, '223993': KoreaV82.JUL}, {'m': [['2026-06', 1.0, 1.0, None, None, None]]})
        self.assertNotIn('2026-08', {r[0] for r in out['m']}, 'wartość ponad 100 bln KRW — nieczytelna, nie liczba')
        self.assertTrue(any(e.startswith('FSS: 1 komunikaty nieczytelne') for e in zd.META['errors']))


class ThailandV86(unittest.TestCase):
    """v86 / v86.1: ThaiBMA — dzień w toku pominięty, zakończony dzień bez części popołudniowej = brak; zły dzień = brak (nie blokada);
    ≈ USD kursem Fed; wiersze tylko ze źródła; BOM; kontrole skali, sum i świeżości."""

    @staticmethod
    def row(day, nf, tot=None, st=None, lt=None, ex=0.0, hold=900000.0, p3=1.0):
        tot = nf + ex if tot is None else tot
        st = tot if st is None else st
        lt = tot - st if lt is None else lt
        return {'Asof': day + 'T00:00:00', 'DisplayAsof': day + 'T00:00:00', 'P1Net': tot, 'P2Net': 0.0, 'P3Net': p3, 'ShortTermTrade': st,
                'LongTermTrade': lt, 'TotalNetTrade': tot, 'ExpireToday': ex, 'NetFlow': nf, 'NetHolding': hold}

    @classmethod
    def hist(cls, *extra):
        """35 wcześniejszych dni (sierpień i wrzesień do 15.09) + podane wiersze — źródło oddaje historię malejąco."""
        base = [(datetime.date(2026, 8, 1) + datetime.timedelta(days=i)).isoformat() for i in range(46)]
        rows = {d: cls.row(d, 100.0) for d in base}
        for r in extra:
            rows[r['Asof'][:10]] = r
        return [rows[k] for k in sorted(rows, reverse=True)]

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_parse_in_progress_gaps_and_duplicates(self):
        R = self.row
        src = [R('2026-09-25', -3648.0, p3=None), R('2026-09-24', 3216.0, st=1695.0), R('2026-09-24', 1.0),
               dict(R('2026-09-23', 1.0), NetFlow=None, TotalNetTrade=None), R('2026-09-22', -796.0, ex=3.0), R('2026-09-21', 5.0, p3=None)]
        out = zd.parse_thbma(src)
        self.assertEqual([r[0] for r in out], ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'], 'dzień w toku pominięty; rosnąco')
        self.assertEqual(out[0][1:], [None] * 6, 'zakończony dzień bez części popołudniowej = brak, nie pominięcie')
        self.assertEqual(out[2][1:], [None] * 6, 'dzień bez sum = brak')
        self.assertEqual(out[3][:4], ['2026-09-24', 3216.0, 3216.0, 1695.0], 'powtórzona data — pierwszy wiersz')
        self.assertTrue(any(n.startswith('ThaiBMA: różne wiersze dla tej samej daty (wzięty pierwszy): 2026-09-24') for n in zd.META['notes']))
        self.assertTrue(any('pokazany jako brak): 2026-09-21, 2026-09-23' in n for n in zd.META['notes']), zd.META['notes'])
        with self.assertRaises(RuntimeError):
            zd.parse_thbma([R('2026-09-25', 1.0, p3=None)])
        with self.assertRaises(RuntimeError):
            zd.parse_thbma({'error': 'x'})

    def run_(self, rows, key='KLUCZ', prev=None, now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc), raw=None):
        seen = []

        def gb(url, headers=None, timeout=60):
            seen.append(url); return raw if raw is not None else json.dumps(rows).encode()

        def gj(url, headers=None):
            seen.append(url)
            assert 'DEXTHUS' in url and 'limit=400' in url, url
            return {'observations': [{'date': '2026-09-18', 'value': '32.50'}, {'date': '2026-08-01', 'value': '.'}]}
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd, '_now_utc', lambda: now):
            return zd.thbma_part(prev, key), seen

    def test_part_usd_rates_old_values_and_checks(self):
        R = self.row
        rows = self.hist(R('2026-09-24', 3216.0), R('2026-09-23', 4612.0, st=4000.0, lt=100.0), R('2026-09-17', 650.0))
        out, seen = self.run_(rows)
        d = {r[0]: r for r in out['d']}
        self.assertEqual(d['2026-09-24'][7:], [round(3216.0 / 32.5, 1), 32.5, '2026-09-18'], 'kurs z najbliższego wcześniejszego dnia')
        self.assertEqual(d['2026-09-17'][7:], [None, None, None], 'dzień przed pierwszym kursem — brak, nie zero')
        self.assertEqual(out['asof'], '2026-09-24'); self.assertEqual(out['url'], zd.THBMA_PAGE)
        self.assertTrue(any(n.startswith('ThaiBMA: sumy niezgodne w dniach: 2026-09-23') for n in zd.META['notes']), zd.META['notes'])
        self.assertEqual(zd.META['errors'], [])
        out2, seen2 = self.run_(rows, key='', prev=out)
        self.assertEqual({r[0]: r for r in out2['d']}['2026-09-24'][7:], [round(3216.0 / 32.5, 1), 32.5, '2026-09-18'], 'bez klucza — przeliczenie z poprzedniego pliku')
        self.assertFalse(any('DEXTHUS' in u for u in seen2))
        rows2 = self.hist(R('2026-09-24', 3300.0))
        out3, _ = self.run_(rows2, key='', prev=dict(out, d=out['d'] + [['2026-07-01', 1.0, 1.0, 1.0, 0.0, 0.0, 900000.0, None, None, None]]))
        self.assertEqual({r[0]: r for r in out3['d']}['2026-09-24'][7:], [None, None, None], 'poprawiona wartość — stare przeliczenie nie pasuje')
        self.assertNotIn('2026-07-01', {r[0] for r in out3['d']}, 'wiersze tylko ze źródła — bez starych dni z pliku')

    def test_scale_bom_json_and_stale(self):
        R = self.row
        with self.assertRaises(RuntimeError):
            self.run_(self.hist(R('2026-09-24', 3.2e6)))
        with self.assertRaises(RuntimeError):
            self.run_(self.hist(R('2026-09-24', 32.0, hold=920.0)))        # najnowszy stan 920 mln THB zamiast ok. 920 mld — zła skala
        zd.META['errors'].clear()
        out, _ = self.run_(self.hist(R('2026-09-24', 3216.0), R('2026-09-02', 2.5e5)))
        bad = {r[0]: r for r in out['d']}['2026-09-02']
        self.assertEqual(bad[1:], [None] * 9, 'stary zły dzień = brak, część działa dalej')
        self.assertTrue(any(e.startswith('ThaiBMA: wartości poza skalą (pokazane jako brak) w dniach: 2026-09-02') for e in zd.META['errors']))
        zd.META['errors'].clear()
        out, _ = self.run_(None, raw=b'\xef\xbb\xbf' + json.dumps(self.hist(R('2026-09-24', 3216.0))).encode())
        self.assertEqual(out['asof'], '2026-09-24', 'znak BOM na początku nie psuje odczytu')
        with self.assertRaisesRegex(RuntimeError, 'nie jest JSON'):
            self.run_(None, raw=b'<html>przerwa techniczna</html>')
        with self.assertRaisesRegex(RuntimeError, 'za mało dni'):
            self.run_([R('2026-09-24', 1.0)])
        self.run_(self.hist(R('2026-09-16', 1.0)), now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc))
        self.assertTrue(any(e.startswith('ThaiBMA: brak nowego pełnego dnia po 2026-09-16') for e in zd.META['errors']), zd.META['errors'])

class PolskaV87(unittest.TestCase):
    """v87 / v87.1: MF — nierezydenci w krajowych SPW: kolumny po nazwach, odnośniki po tytułach (zapas: nazwa pliku), kraje opcjonalnie
    (arkusze według miesiąca, udziały ok. 100%), brak kolumn i niezgodne sumy = błąd, kontrole skali i świeżości."""
    NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    @classmethod
    def book(cls, sheets):
        """{nazwa arkusza: [[komórki wiersza]]} → .xlsx (tekst inline, liczby jako liczby)."""
        import io, zipfile
        from xml.sax.saxutils import escape
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('xl/workbook.xml', f'<workbook {cls.NS}><sheets>' + ''.join(
                f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheets)) + '</sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(
                f'<Relationship Id="rId{i + 1}" Type="x" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets))) + '</Relationships>')
            for i, rows in enumerate(sheets.values()):
                xml = ''
                for rn, row in enumerate(rows, 1):
                    cells = ''.join((f'<c r="{chr(65 + c)}{rn}"><v>{v}</v></c>' if isinstance(v, (int, float)) else
                                     f'<c r="{chr(65 + c)}{rn}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>') for c, v in enumerate(row) if v is not None)
                    xml += f'<row r="{rn}">{cells}</row>'
                z.writestr(f'xl/worksheets/sheet{i + 1}.xml', f'<worksheet {cls.NS}><sheetData>{xml}</sheetData></worksheet>')
        return buf.getvalue()

    TY = ['Data', 'Banki', 'Banki centralne', 'Instytucje publiczne', 'Zakłady ubezpieczeniowe', 'Fundusze emerytalne', 'Fundusze inwestycyjne',
          'Fundusze hedgingowe', 'Gospodarstwa domowe', 'Przedsiębiorstwa niefinansowe', 'Inne podmioty', 'Rachunki zbiorcze', 'Razem']
    RG = ['Data', 'Europa - kraje strefy euro', 'Europa - kraje UE spoza strefy euro', 'Europa - kraje spoza UE', 'Afryka',
          'Ameryka Południowa (w tym Karaiby)', 'Ameryka Północna', 'Australia i Oceania', 'Azja (bez Bliskiego Wschodu)', 'Bliski Wschód',
          'Rachunki zbiorcze', 'Razem']

    def st_book(self, tot_jul=204131.8, swap=False, drop=None):
        ser = {'2026-05-31': 46173, '2026-06-30': 46203, '2026-07-31': 46234}
        vals = {'2026-05-31': 206240.0, '2026-06-30': 198955.2, '2026-07-31': tot_jul}
        head = list(self.TY)
        rows_t = []
        for d, s in ser.items():
            v = vals[d]
            parts = [v * 0.1, v * 0.05, 0, 0, 0, v * 0.35, 0, 0, 0, 0, v * 0.5]
            rows_t.append([s] + parts + [sum(parts)])
        if swap:
            head = [head[0], head[2], head[1]] + head[3:]
            rows_t = [[r[0], r[2], r[1]] + r[3:] for r in rows_t]
        if drop:
            i = head.index(drop); head = head[:i] + head[i + 1:]; rows_t = [r[:i] + r[i + 1:] for r in rows_t]
        rows_t = [['Struktura podmiotowa nierezydentów w krajowych SPW (mln zł)'], [], head] + rows_t
        rows_r = [['Struktura geograficzna'], [], self.RG] + [[s] + [vals[d] * 0.3, 0, 0, 0, 0, vals[d] * 0.2, 0, 0, 0, vals[d] * 0.5, vals[d]] for d, s in ser.items()]
        rows_b = [['x'], [], ['Data', 'Banki', 'Razem']] + [[s, 0, vals[d] - 100] for d, s in ser.items()]
        rows_s = [['x'], [], ['Data', 'Banki', 'Razem']] + [[s, 0, 100] for d, s in ser.items()]
        return self.book({'Legenda': [['Tabele']], 'Razem_podmiot': rows_t, 'Razem_region': rows_r,
                          'Obligacje skarbowe_podmiot': rows_b, 'Bony skarbowe_podmiot': rows_s})

    def kr_book(self, order=('Lipiec2026(July2026)', 'Czerwiec2026(June2026)', 'Maj2026(May2026)'), others=0.2):
        def sheet(jp):
            return [['Kraje o udziale w zadłużeniu nierezydentów* w krajowych SPW większym niż 1% / Countries…'],
                    ['Kraje/Countries', 'Wartość nominalna', 'Udział'], [],
                    ['Japonia/Japan', jp, 0.5], ['Holandia/Netherlands (the)', 7678.98, 0.3], ['Pozostałe kraje/Others', 7043.42, others],
                    ['Suma/Total*', 92030.03, 1.0], ['Rachunki zbiorcze/Omnibus accounts', 97615.88, '-'], ['Banki centralne/Central banks', 14485.92, '-'],
                    ['Razem nierezydenci/Non-residents total', 204131.82, '-']]
        vals = {'Lipiec2026(July2026)': 18031.64, 'Czerwiec2026(June2026)': 17382.46, 'Maj2026(May2026)': 1.0}
        return self.book({n: sheet(vals[n]) for n in order})

    PAGE = ('<a class="file-download" href="/attachment/e73b4c6d-7ad4-4feb-81ce-dde6f293c39f" download>Struktura podmiotowa zadłużenia wobec nierezydentów '
            'w krajowych obligacjach rynkowych po seriach<br/><span>x.xls</span></a>'
            '<a class="file-download" href="/attachment/9ab3f0b9-1742-4b00-a6d9-a6a7753368ee" target="_blank" download\naria-label="Pobierz">\n'
            'Struktura podmiotowa zadłużenia wobec nierezydentów w krajowych SPW<br/>\n<span class="extension">Struktura&#8203;_nierezydentow07.xlsm</span></a>'
            '<a class="file-download" href="/attachment/fc49ffc2-3403-4ab7-977b-411ed9964215" download>Zadłużenie wobec nierezydentów w krajowych SPW po krajach'
            '<br/><span>Nierezydenci&#8203;_kraje07.xlsx</span></a>')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def run_(self, st=None, kr=None, page=None, now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
        st, kr, page = st or self.st_book(), kr if kr is not None else self.kr_book(), page or self.PAGE

        def gb(url, headers=None, timeout=60):
            if url == zd.SPW_PAGE:
                return page.encode()
            if url.endswith('9ab3f0b9-1742-4b00-a6d9-a6a7753368ee'):
                return st
            if url.endswith('fc49ffc2-3403-4ab7-977b-411ed9964215'):
                if isinstance(kr, Exception):
                    raise kr
                return kr
            raise AssertionError(url)
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, '_now_utc', lambda: now):
            return zd.build_spw()

    def test_links_by_title_and_by_file_name(self):
        L = zd.spw_links(self.PAGE)
        self.assertEqual(L, {'st': 'https://www.gov.pl/attachment/9ab3f0b9-1742-4b00-a6d9-a6a7753368ee',
                             'kr': 'https://www.gov.pl/attachment/fc49ffc2-3403-4ab7-977b-411ed9964215'}, 'plik „po seriach” pominięty')
        renamed = self.PAGE.replace('Struktura podmiotowa zadłużenia wobec nierezydentów w krajowych SPW<br/>', 'Nierezydenci — struktura<br/>')
        self.assertEqual(zd.spw_links(renamed)['st'], 'https://www.gov.pl/attachment/9ab3f0b9-1742-4b00-a6d9-a6a7753368ee', 'zmieniony tytuł — plik po nazwie')

    def test_build_months_groups_countries(self):
        out = self.run_()
        self.assertEqual([r[0] for r in out['m']], ['2026-05', '2026-06', '2026-07'])
        self.assertEqual(out['m'][-1], ['2026-07', 204131.8, 204031.8, 100.0])
        self.assertAlmostEqual(dict(out['t']['cb'])['2026-07'], 204131.8 * 0.05)
        self.assertAlmostEqual(dict(out['t']['bank'])['2026-07'], 204131.8 * 0.1, msg='„Banki” to nie „Banki centralne”')
        self.assertAlmostEqual(dict(out['r']['omni'])['2026-07'], 204131.8 * 0.5)
        self.assertEqual([k['m'] for k in out['kr']], ['2026-07', '2026-06'], 'dwa najnowsze arkusze')
        self.assertEqual(out['kr'][0]['c'][0], ['Japonia', 'Japan', 18031.64, 50.0])
        self.assertEqual(out['kr'][0]['c'][1][1], 'Netherlands', 'bez „(the)”')
        self.assertEqual([c[0] for c in out['kr'][0]['c']], ['Japonia', 'Holandia', 'Pozostałe kraje'], 'bez sum, rachunków zbiorczych i banków centralnych')
        self.assertEqual(zd.META['errors'], []); self.assertEqual(zd.META['notes'], [])
        out2 = self.run_(st=self.st_book(swap=True), kr=self.kr_book(order=('Maj2026(May2026)', 'Lipiec2026(July2026)', 'Czerwiec2026(June2026)')))
        self.assertAlmostEqual(dict(out2['t']['cb'])['2026-07'], 204131.8 * 0.05, msg='kolumny po nazwach, nie po kolejności')
        self.assertEqual([k['m'] for k in out2['kr']], ['2026-07', '2026-06'], 'arkusze według miesiąca, nie kolejności w pliku')

    def test_countries_optional_columns_scale_and_stale(self):
        out = self.run_(kr=RuntimeError('HTTP Error 503'))
        self.assertNotIn('kr', out); self.assertTrue(any(e.startswith('MF SPW kraje: HTTP Error 503') for e in zd.META['errors']))
        zd.META['errors'].clear()
        out = self.run_(kr=self.kr_book(others=0.9))
        self.assertNotIn('kr', out); self.assertTrue(any('sumują się do 170.0%' in e for e in zd.META['errors']), zd.META['errors'])
        zd.META['errors'].clear()
        self.run_(st=self.st_book(drop='Fundusze hedgingowe'))
        self.assertIn('MF SPW: brak kolumn: hf', zd.META['errors'])
        zd.META['errors'].clear()
        with self.assertRaises(RuntimeError):
            self.run_(st=self.st_book(tot_jul=204.1))          # 204 mln zł zamiast 204 mld — zła skala
        zd.META['errors'].clear()
        self.run_(now=datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.timezone.utc))
        self.assertIn('MF SPW: brak nowego miesiąca po 2026-07', zd.META['errors'])
        with self.assertRaises(RuntimeError):
            self.run_(page='<a href="/attachment/x">inne</a>')

class MeksykV88(unittest.TestCase):
    """v88 / v88.1: Banxico — kolumny po kodach serii, „N/E” = brak, POST bez tokenu, rodzaje papierów (Udibonos × UDI),
    ≈ USD kursem Fed z dnia danych, kontrole skali i świeżości."""
    CSV = ('\r\n"Banco de México"\r\n\r\n"Valores en circulación"\r\n\r\n"Título","GUBERNAMENTAL, Total en Circulación (I + II)","GUBERNAMENTAL, Residentes en el Extranjero (II)"\r\n'
           '"Periodicidad","Diaria","Diaria"\r\n"Fecha","SF65219","SF65218"\r\n"11/09/2026","16096101.67","1789166.23"\r\n'
           '"14/09/2026","16097115.95","1788646.44"\r\n"10/09/2026","16090000.00","N/E"\r\n"12/09/2026","",""\r\n')
    CSV2 = ('"Fecha","SF65218","SF65219","SF65137","SF65046","SF65107","SP68257"\r\n'
            '"14/09/2026","1788646.44","16097115.95","1512900.00","202500.00","5200.00","8.84"\r\n'
            '"15/09/2026","N/E","N/E","N/E","N/E","N/E","8.845"\r\n')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def test_parse_by_codes_gaps_and_instruments(self):
        out = zd.parse_bmx(self.CSV)
        self.assertEqual(out, [['2026-09-11', 1789166.23, 16096101.67, None, None, None], ['2026-09-14', 1788646.44, 16097115.95, None, None, None]],
                         'po kodach, nie po kolejności; N/E i puste = brak dnia; bez serii rodzajów — brak, nie zero')
        out2 = zd.parse_bmx(self.CSV2)
        self.assertEqual(out2, [['2026-09-14', 1788646.44, 16097115.95, 1512900.0, 202500.0, round(5200.0 * 8.84, 2)]], 'Udibonos: mln UDI × wartość UDI; dzień z samą UDI pominięty')
        with self.assertRaises(RuntimeError):
            zd.parse_bmx('"Fecha","SF65219"\r\n"14/09/2026","1"\r\n')

    def run_(self, csv_text, key='KLUCZ', now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)):
        seen = []

        def pb(url, form, timeout=90):
            seen.append((url, form)); return csv_text.encode('latin-1')

        def gj(url, headers=None):
            assert 'DEXMXUS' in url, url
            return {'observations': [{'date': '2026-09-18', 'value': '18.25'}, {'date': '2026-09-11', 'value': '17.11'}, {'date': '2026-09-17', 'value': '.'}]}
        with mock.patch.object(zd, 'post_bytes', pb), mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd, '_now_utc', lambda: now):
            return zd.build_meksyk(key), seen

    def test_build_form_fx_scale_and_stale(self):
        out, seen = self.run_(self.CSV)
        url, form = seen[0]
        self.assertEqual(url, zd.BMX_URL); self.assertEqual(form['series'], ['SF65218', 'SF65219', 'SF65137', 'SF65046', 'SF65107', 'SP68257'])
        self.assertEqual(form['anoInicial'], '2024', 'dwa lata wstecz — w styczniu też jest koniec roku'); self.assertIn('formatoCSV.x', form)
        self.assertNotIn('token', json.dumps(form).lower())
        self.assertEqual(out['asof'], '2026-09-14'); self.assertEqual(out['fx'], [17.11, '2026-09-11'], 'kurs z dnia danych albo wcześniejszego, nie najnowszy')
        self.assertEqual(out['url'], zd.BMX_PAGE); self.assertEqual(zd.META['errors'], [])
        out2, _ = self.run_(self.CSV, key='')
        self.assertNotIn('fx', out2, 'bez klucza — bez przeliczenia, nie zero')
        with self.assertRaises(RuntimeError):
            self.run_(self.CSV.replace('1788646.44', '1788.64'))          # 1,8 mld MXN zamiast 1,8 bln — zła skala
        with self.assertRaises(RuntimeError):
            self.run_(self.CSV.replace('16097115.95', '1000000.00'))      # nierezydenci więcej niż całość
        zd.META['errors'].clear()
        self.run_(self.CSV, now=datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.timezone.utc))
        self.assertIn('Banxico: brak nowego dnia po 2026-09-14', zd.META['errors'])
        zd.META['notes'].clear()
        self.run_(self.CSV2.replace('"1512900.00"', '"1712900.00"'))
        self.assertTrue(any(n.startswith('Banxico: Bonos M + Cetes + Udibonos większe niż całość') for n in zd.META['notes']), zd.META['notes'])

class MeksykFormatV882(unittest.TestCase):
    """v88.2: meksyk.json bez kolumn rodzajów papierów jest pobierany od razu; plik w nowym formacie — z pamięci."""

    def test_old_format_rebuilt_new_format_cached(self):
        for row, rebuilt in ((['2026-09-14', 1788646.44, 16097115.95], True), (['2026-09-14', 1788646.44, 16097115.95, 1.0, 1.0, 1.0], False)):
            zd.META['errors'].clear(); zd.META['ok'].clear(); saved = {}
            prev = {'at': _iso(30), 'd': [row]}
            new = {'at': zd.NOW, 'd': [row + [None] * (6 - len(row))]}
            offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                    for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                              'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
            [p.start() for p in offs]
            try:
                with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                        mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), \
                        mock.patch.object(zd, 'previous', lambda name: prev if name == 'meksyk' else None), \
                        mock.patch.object(zd, 'build_meksyk', return_value=new):
                    zd.main()
            finally:
                [p.stop() for p in offs]
            self.assertIs(saved['meksyk'], new if rebuilt else prev, 'stary format — od razu nowy plik' if rebuilt else 'nowy format — z pamięci')


class TrendyV89(unittest.TestCase):
    """v89: TRENDY — tydzień porównany z 4 poprzednimi; brak w oknie = brak wyniku; stan „za stare” po zwykłym opóźnieniu źródła."""

    @staticmethod
    def blocks(*sums, size=5):
        """Sumy tygodni od najstarszego → wartości dzienne (każdy dzień = suma / size)."""
        return [s / size for s in sums for _ in range(size)]

    @staticmethod
    def weekdays(end, n):
        """n kolejnych dni roboczych kończących się na `end` (data ISO)."""
        d, out = datetime.date.fromisoformat(end), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d -= datetime.timedelta(days=1)
        return out[::-1]

    def test_state_rules(self):
        prev = [100, 100, 90, 110, 100, 80, 120, 100]            # od najstarszego; 4 ostatnie przed bieżącym: 100, 80, 120, 100
        t = zd.trend_state(self.blocks(*prev, 300), 5)
        self.assertEqual((t['st'], t['n'], t['lc']), ('in_up', 8, False))
        self.assertAlmostEqual(t['base'], 100); self.assertAlmostEqual(t['d'], 8.0, msg='rozrzut 11,95 < 1/4 typowego tygodnia (25) — próg 25')
        self.assertTrue(t['x'], '|d| ≥ 3 przy 8 tygodniach historii')
        self.assertEqual(zd.trend_state(self.blocks(*[200] * 8, 120), 5)['st'], 'in_down', 'napływ wyraźny, ale o 1,6 rozrzutu słabszy niż zwykle')
        self.assertEqual(zd.trend_state(self.blocks(*[200] * 8, 190), 5)['st'], 'in_flat')
        self.assertEqual(zd.trend_state(self.blocks(*[-100] * 8, -20), 5)['st'], 'out_stop', 'zwykle odpływ, w tym tygodniu prawie nic (v90)')
        self.assertEqual(zd.trend_state(self.blocks(*[-100] * 8, -300), 5)['st'], 'out_up')
        self.assertEqual(zd.trend_state(self.blocks(*[-100] * 8, 200), 5)['st'], 'in_rev', 'napływ po tygodniach odpływu — nie „większy niż zwykle”')
        self.assertEqual(zd.trend_state(self.blocks(*[10, -10] * 4, 200), 5)['st'], 'in_new', 'napływ po okresie bez wyraźnego kierunku')
        v = self.blocks(*[100] * 8) + [150, -20, -20, -20, -20]          # suma 70 (≥ 0,5 i < 1 typowego tygodnia), tylko 1 z 5 sesji na plus
        self.assertEqual(zd.trend_state(v, 5)['st'], 'mixed', 'duża suma z jednego dnia — tydzień niejednolity, nie „bez zmian”')
        v = self.blocks(*[100] * 8) + [200, -30, -30, -30, -10]          # suma 100 = typowy tydzień: kierunek wyraźny mimo dni
        self.assertEqual(zd.trend_state(v, 5)['st'], 'in_flat')
        t = zd.trend_state(self.blocks(100, 100, 100, 300), 5)
        self.assertEqual((t['st'], t['n']), ('short', 3), 'mniej niż 4 poprzednie tygodnie')
        self.assertEqual(zd.trend_state(self.blocks(*[100] * 8, 300)[:-1] + [None], 5)['st'], 'gap', 'brak dnia w bieżącym tygodniu — brak wyniku')
        t = zd.trend_state(self.blocks(*[100] * 5, 300), 5)
        self.assertEqual((t['st'], t['n'], t['lc'], t['x']), ('in_dir', 5, True, False), '5 tygodni: tylko kierunek, bez oceny siły i bez „wyjątkowo”')
        self.assertEqual(zd.trend_state([None] * 5 + self.blocks(*[100] * 4, 300), 5)['n'], 4)
        t = zd.trend_state([0.0] * 45, 5)
        self.assertEqual((t['st'], t.get('d')), ('none', None), 'same zera — bez dzielenia przez zero')
        self.assertEqual(zd.trend_state([10] * 8 + [50], 1)['st'], 'in_up', 'dane tygodniowe: bez warunku większości dni')
        self.assertEqual(zd.trend_state(self.blocks(*[70] * 8, 210, size=7), 7)['st'], 'in_up')
        self.assertEqual(zd.trend_state([float('nan')] + self.blocks(*[100] * 8, 300)[1:], 5)['n'], 7, 'NaN to brak, nie liczba')

    def test_calendar_gaps(self):
        ds = self.weekdays('2026-09-18', 45)
        v = self.blocks(*[100] * 8, 300)
        self.assertEqual(zd.trend_state(v, 5, ds, 11)['st'], 'in_up')
        ds2 = ds[:-5] + ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-10-05']   # ostatnie 5 wierszy na 15 dniach
        self.assertEqual(zd.trend_state(v, 5, ds2, 11)['st'], 'gap', 'brak wierszy z kilku dni w tygodniu — brak wyniku, nie ciche sklejenie')
        tw = {'d': [[d, 10.0, 0, 0, 0, 0.3, d] for d in ds], 'empty': []}
        del tw['d'][-3]                                                  # dzień roboczy bez wiersza (nieudane pobranie)
        now = datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(zd, '_now_utc', return_value=now):
            self.assertEqual({r['id']: r['st'] for r in zd.build_trendy({'obce': {'tw': tw}})['f']}['tw'], 'gap')
            tw['empty'] = [ds[-3]]                                       # ten sam dzień jako znany dzień bez sesji — nie jest brakiem
            r = {r['id']: r for r in zd.build_trendy({'obce': {'tw': tw}})['f']}['tw']
            self.assertEqual((r['st'], r['n'], r['date']), ('in_dir', 7, '2026-09-18'), '44 sesje = bieżący tydzień + 7 pełnych: tylko kierunek')

    def test_helpers(self):
        self.assertEqual(zd.trend_streak([1, -2, 3, 4, 5]), (3, 1))
        self.assertEqual(zd.trend_streak([1, None, -3, -4]), (2, -1))
        self.assertEqual(zd.trend_streak([2, 0]), (0, 0), 'zero przerywa serię')
        self.assertIsNone(zd.trend_day_z([1.0] * 30 + [5.0]), 'za mało sesji do porównania dnia')
        v = [(-1) ** i * 10.0 for i in range(60)] + [60.0]
        self.assertAlmostEqual(zd.trend_day_z(v), 60 / zd._sd(v[:-1]), places=6)
        self.assertEqual(zd.wilson(22, 53), (29.3, 54.9), '22 z 53 — policzone też ręcznie')
        self.assertEqual(str(zd.wilson(0, 10)[0]), '0.0', 'bez „-0.0”')
        self.assertEqual(zd.wilson(0, 0), (None, None))
        lo, hi = zd.wilson(195, 418, n_eff=50)
        self.assertTrue(lo < 40 and hi > 55, 'powiązane rynki: przedział liczony na tygodnie, szerszy')
        ds = self.weekdays('2026-09-11', 60)                            # 12 pełnych tygodni kalendarzowych
        v = [20.0 if (i // 5) % 2 == 0 else -20.0 for i in range(60)]  # tydzień na plus, tydzień na minus, …
        k, n, a, b = zd.trend_persist(ds, v, datetime.date(2026, 9, 25))
        self.assertEqual((k, n), (0, 7), 'kierunek zawsze się odwracał; pierwsze 4 tygodnie tylko do porównania')
        self.assertEqual((a, b), ('2026-07-20', '2026-09-07'))
        k2, n2, _, _ = zd.trend_persist(ds, v, datetime.date(2026, 9, 9))
        self.assertEqual(n2, 6, 'tydzień bieżący (niezakończony) nie jest liczony')
        ds, v = zd._tr_weeks(['2026-09-04', '2026-09-18'], [1, 3], n=3)
        self.assertEqual((ds, v), (['2026-09-04', '2026-09-11', '2026-09-18'], [1, None, 3]), 'brakujący tydzień = brak, nie sklejenie')
        ds, v = zd._tr_weeks(['2026-09-01', '2026-09-08', '2026-09-14'], [1, 2, 3], n=3)
        self.assertEqual((ds, v), (['2026-09-01', '2026-09-08', '2026-09-14'], [1, 2, 3]), 'raport w poniedziałek po święcie — ten sam tydzień')
        self.assertEqual(zd._bdays(datetime.date(2026, 9, 18), datetime.date(2026, 9, 21)), 1, 'piątek → poniedziałek = 1 dzień roboczy')
        self.assertEqual(zd._bdays(datetime.date(2026, 9, 18), datetime.date(2026, 9, 22), {'2026-09-21'}), 1, 'znany dzień bez sesji nie postarza danych')
        self.assertTrue(zd._cftc_roll('2026-09-15')); self.assertFalse(zd._cftc_roll('2026-10-13'))
        self.assertFalse(zd._isnum(float('nan'))); self.assertFalse(zd._isnum(float('inf'))); self.assertFalse(zd._isnum(True))

    def S(self):
        ses = self.weekdays('2026-09-18', 45)
        th = [[d, 10.0, 10.0, 0, 0, 0, 1000000.0, 0.3, 33.0, d] for d in ses[:-5]] + [[d, 100.0, 100.0, 0, 0, 0, 1000000.0, 3.0, 33.0, d] for d in ses[-5:]]
        tw = [[d, -50.0, 0, 0, 0, -1.6, d] for d in ses]
        tw[-3][1] = None
        mx = [[d, 1000.0 + i, 5000.0] for i, d in enumerate(ses)]
        return {'obce': {'th': {'d': th}, 'tw': {'d': tw}}, 'meksyk': {'d': mx, 'fx': [10.0, ses[-1]]}}

    def test_build_rows(self):
        now = datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)     # poniedziałek po ostatniej sesji
        with mock.patch.object(zd, '_now_utc', return_value=now):
            out = zd.build_trendy(self.S())
            by = {r['id']: r for r in out['f']}
            th = by['th']
            self.assertEqual((th['st'], th['w'], th['base'], th['n'], th['cur'], th['wu'], th['du']), ('in_up', 500.0, 50.0, 8, 'THB', 15.0, 13.5))
            self.assertEqual((th['ph'], th['s'], th['sg'], th['date'], th['age'], th['x']), (0.05, 45, 1, '2026-09-18', 3, True))
            self.assertEqual(by['tw']['st'], 'gap', 'brak jednej sesji w tygodniu — bez stanu, nie zero')
            self.assertNotIn('w', by['tw']); self.assertFalse(by['tw']['x'])
            mx = by['mx']
            self.assertEqual((mx['m'], mx['cur'], mx['w'], mx['wu'], mx['st'], mx['n']), ('stock', 'MXN', 5.0, 0.5, 'in_dir', 7),
                             'Meksyk: zmiana stanu dzień do dnia (44 zmiany z 45 dni) — 7 tygodni historii, tylko kierunek')
            self.assertEqual([b['id'] for b in out['b']], ['th', 'mx', 'ob']); self.assertEqual(out['rules']['base_max'], 8)
            self.assertEqual(out['b'][0]['from'][:4], '2026')
        with mock.patch.object(zd, '_now_utc', return_value=now + datetime.timedelta(days=4)):   # piątek: 4 dni robocze po danych
            r = {r['id']: r for r in zd.build_trendy(self.S())['f']}['th']
            self.assertEqual((r['st'], r['x']), ('stale', False), 'za stare — bez oceny i bez „wyjątkowo”')
        self.assertEqual(zd.build_trendy({})['f'], [], 'bez plików — pusta lista, nie błąd')
        self.assertEqual(zd.build_trendy(None)['p'], [])
        zd.META['notes'].clear()
        with mock.patch.object(zd, '_now_utc', return_value=now):
            out = zd.build_trendy({'obce': {'th': 'zepsute', 'tw': self.S()['obce']['tw']}})
        self.assertEqual([r['id'] for r in out['f']], ['tw'], 'zepsuta część jednego źródła nie usuwa pozostałych')
        self.assertTrue(any(n.startswith('trendy th:') for n in zd.META['notes']), zd.META['notes'])

    def test_japan_weekly_usd(self):
        weeks = [(datetime.date(2026, 7, 18) + datetime.timedelta(days=7 * i)).isoformat() for i in range(9)]
        mof = [{'from': w, 'to': w, 'liabilities': {'equity_net': 1000.0, 'ltdebt_net': None if i == 3 else 500.0}} for i, w in enumerate(weeks)]
        S = {'instytucje': {'mof': {'d': mof}}, 'kursy': {'m': {'JPY': [['2026-07', 160.0], ['2026-08', 170.0]], 'USD': [['2026-07', 1.0], ['2026-08', 1.0]]}}}
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 20, 10, 0, tzinfo=datetime.timezone.utc)):
            by = {r['id']: r for r in zd.build_trendy(S)['f']}
        eq = by['jp_eq']
        self.assertEqual((eq['w'], eq['cur'], eq['fxm'], eq['n'], eq['date'], eq['st']), (100.0, 'JPY', '2026-08', 8, '2026-09-12', 'in_flat'))
        self.assertEqual(eq['wu'], round(100.0 * 1000 / 170.0, 1), 'mld JPY → mln USD kursem sierpnia (ostatni znany ≤ wrzesień)')
        self.assertEqual(by['jp_bd']['n'], 4, 'brak tygodnia kończy porównanie: bieżący + 4 poprzednie (nie zero)')
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 20, 10, 0, tzinfo=datetime.timezone.utc)):
            self.assertNotIn('wu', {r['id']: r for r in zd.build_trendy({'instytucje': S['instytucje']})['f']}['jp_eq'], 'bez kursów — bez ≈ USD, nie zero')

    def test_cftc_and_stablecoins(self):
        dates = [(datetime.date(2026, 6, 23) + datetime.timedelta(days=7 * i)).isoformat() for i in range(13)]
        dates[-1] = '2026-09-14'                                         # święto we wtorek — raport z poniedziałku
        cf = {'markets': {'usd': {'hist': {'dates': dates, 'lev_funds': [100 * i for i in range(12)] + [2000]}}}}
        now = datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)
        dd = [[(datetime.date(2026, 7, 1) + datetime.timedelta(days=i)).isoformat(), 1e9 + 1e6 * i] for i in range(83)]   # do 2026-09-21 (dziś)
        dd[-1][1] = 5e9                                                  # dzisiejszy, niezamknięty punkt — pomijany
        with mock.patch.object(zd, '_now_utc', return_value=now):
            by = {r['id']: r for r in zd.build_trendy({'cftc': cf, 'krypto': {'stabh': {'dd': dd}}})['f']}
        u = by['cf_usd']
        self.assertEqual((u['date'], u['w'], u['n'], u['st'], u['roll'], u['x']), ('2026-09-14', 900, 8, 'in_up', True, False),
                         'poniedziałkowy raport w tym samym tygodniu; tydzień rolowania — bez „wyjątkowo”')
        s = by['stab']
        self.assertEqual((s['date'], s['w'], s['st'], s['sz']), ('2026-09-20', 7.0, 'in_flat', 7), 'stablecoiny: tylko zamknięte dni (bez dzisiejszego)')

    def test_prices_and_crypto(self):
        days = [d for d in (datetime.date(2025, 7, 1) + datetime.timedelta(days=i) for i in range(450)) if d.weekday() < 5 and d <= datetime.date(2026, 9, 18)]
        d = [[x.isoformat(), 100.0 + (8 if (i // 5) % 2 else 0) + i * 0.05] for i, x in enumerate(days)]
        mk = {'cols': ['sym', 'mcap', 'p24h', 'p7d', 'p30d', 'p1y'],
              'rows': [['btc', 2e12, 1, 8.0, 7.0, 50], ['eth', 5e11, 1, -6.0, 8.0, 20], ['sol', 9e10, 1, 1.0, 2.0, 3], ['btc', 1e6, 0, -50.0, -50.0, 0]]}
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)):
            out = zd.build_trendy({'ceny': {'q': {'SPY': {'d': d}, 'EWC': {'d': d[:20]}}}, 'krypto': {'at': '2026-09-25T08:00:00+00:00', 'mk': mk}})
        by = {r['id']: r for r in out['p']}
        self.assertNotIn('EWC', by, 'za krótka historia cen — bez wiersza')
        self.assertIn(by['SPY']['st'], ('up_cont', 'dn_fade', 'up_new', 'flat', 'dn_cont', 'up_fade', 'dn_new'))
        self.assertEqual(by['BTC']['st'], 'up_new', 'BTC: +8% w tygodniu po −0,9% w 23 dniach; pierwszy wiersz BTC, nie podróbka')
        self.assertEqual(by['BTC']['pr'], -0.93)
        self.assertEqual(by['ETH']['st'], 'up_fade', 'ETH: −6% po +14,9% w 23 dniach — w dół po wcześniejszych wzrostach')
        self.assertEqual(by['SOL']['st'], 'flat', 'SOL: +1% < 4,5% — bez wyraźnego ruchu')
        b = out['b'][0]
        self.assertEqual((b['id'], b['kind'], b['n'] > 0, b['weeks'], b['n']), ('px', 'price', True, b['n'], b['n']), 'jeden rynek: tygodni tyle co par')
        self.assertTrue(b['to'] < '2026-09-21', 'bez tygodnia bieżącego')

    def test_main_writes_trendy_from_this_run(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); saved = {}
        offs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True)
                for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy',
                          'build_kursy', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        [p.start() for p in offs]
        def fake_save(name, obj):
            saved[name] = obj; zd.SAVED[name] = obj
        try:
            with mock.patch.dict(os.environ, {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': ''}, clear=False), \
                    mock.patch.object(zd, 'save', fake_save), mock.patch.object(zd, 'previous', lambda name: None), \
                    mock.patch.object(zd, 'build_obce', return_value=self.S()['obce']), \
                    mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)):
                zd.SAVED['meksyk'] = {'d': [['2026-01-01', 1.0]] * 3}     # pozostałość po innym przebiegu nie może trafić do TRENDÓW
                zd.main()
        finally:
            [p.stop() for p in offs]
        self.assertIs(zd.META['ok']['trendy'], True)
        ids = [r['id'] for r in saved['trendy']['f']]
        self.assertIn('th', ids); self.assertNotIn('mx', ids, 'SAVED czyszczony na początku przebiegu')
        self.assertLess(list(saved).index('trendy'), list(saved).index('meta'), 'TRENDY przed meta — błąd TRENDÓW widać w meta')

    def test_stabh_keeps_70_days(self):
        j = [{'date': str(1780000000 + i * 86400), 'totalCirculatingUSD': {'peggedUSD': 1e9 + i}} for i in range(100)]
        s = zd.parse_stabh(j)
        self.assertEqual(len(s['dd']), 71); self.assertEqual(s['dd'][-1][1], 1000000099)
        self.assertEqual(s['dd'][-1][0], datetime.datetime.fromtimestamp(1780000000 + 99 * 86400, datetime.timezone.utc).date().isoformat())


class FunduszeV90(unittest.TestCase):
    """v90: fundusze ETF w USA — przepływ = zmiana liczby jednostek × NAV z plików State Street i iShares (bez klucza)."""

    XML = ('<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           '<ss:Worksheet ss:Name="Holdings"><ss:Table><ss:Row><ss:Cell><ss:Data ss:Type="String">AT&T</ss:Data></ss:Cell></ss:Row></ss:Table></ss:Worksheet>'
           '<ss:Worksheet ss:Name="Historical"><ss:Table>'
           '<ss:Row><ss:Cell><ss:Data ss:Type="String">As Of</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">NAV per Share</ss:Data></ss:Cell>'
           '<ss:Cell><ss:Data ss:Type="String">Ex-Dividends</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Shares Outstanding</ss:Data></ss:Cell></ss:Row>'
           '{rows}</ss:Table></ss:Worksheet></ss:Workbook>')

    @classmethod
    def xml(cls, rows):
        r = ''.join(f'<ss:Row><ss:Cell><ss:Data ss:Type="String">{d}</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="Number">{n}</ss:Data></ss:Cell>'
                    f'<ss:Cell><ss:Data ss:Type="String">--</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="{"Number" if s != "--" else "String"}">{s}</ss:Data></ss:Cell></ss:Row>'
                    for d, n, s in rows)
        return cls.XML.format(rows=r).encode()

    @staticmethod
    def days(n, end='2026-09-24'):
        d, out = datetime.date.fromisoformat(end), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d -= datetime.timedelta(days=1)
        return out[::-1]

    def test_parse_ishares_and_screener(self):
        ds = self.days(25)
        rows = [(d.strftime('%b %d, %Y'), 100.0 + i, 1000000 + 1000 * i) for i, d in enumerate(ds)][::-1] + [('Jan 03, 2000', 50.0, '--')]
        h = zd.parse_ishares_hist(self.xml(rows))
        self.assertEqual(len(h), 25, 'wiersz z „--” pominięty; gołe „&” w innym arkuszu nie psuje pliku')
        self.assertEqual(h[-1], ['2026-09-24', 124.0, 1024000]); self.assertEqual(h[0][0], ds[0].isoformat())
        with self.assertRaises(RuntimeError):
            zd.parse_ishares_hist(self.xml(rows[:5]))
        j = {'239726': {'localExchangeTicker': 'IVV', 'portfolioId': 239726, 'navAmount': {'r': 770.800542}, 'navAmountAsOf': {'r': 20260924},
                        'totalNetAssetsFund': {'r': 882219760864.18}, 'totalNetAssetsFundAsOf': {'r': 20260924}},
             '239623': {'localExchangeTicker': 'EFA', 'navAmount': {'r': 90.0}, 'navAmountAsOf': {'r': 20260924},
                        'totalNetAssetsFund': {'r': 9e9}, 'totalNetAssetsFundAsOf': {'r': 20260923}},
             '1': {'localExchangeTicker': 'XYZ', 'navAmount': {'r': 1.0}, 'navAmountAsOf': {'r': 20260924}, 'totalNetAssetsFund': {'r': 1.0}, 'totalNetAssetsFundAsOf': {'r': 20260924}}}
        s = zd.parse_ishares_screener(j)
        self.assertEqual(s, {'IVV': ('239726', '2026-09-24', 770.800542, 1144550000)}, 'różne daty NAV i aktywów — bez liczby; spoza listy — pominięty; v94: pełne tysiące')

    def test_parse_ssga(self):
        rows = {1: {1: 'Fund Name:', 2: 'SPDR® Gold Shares'}, 2: {1: 'Ticker Symbol:', 2: 'GLD®'}, 4: {1: 'Date', 2: 'NAV', 3: 'Shares Outstanding', 4: 'Total Net Assets'}}
        for i, d in enumerate(self.days(25)[::-1]):
            rows[5 + i] = {1: d.strftime('%d-%b-%Y'), 2: str(390.0 - i), 3: f'{3.697E8 - i * 1e5:E}', 4: '1'}
        rows[40] = {1: 'The whole or any part of this work may not be reproduced'}
        with mock.patch.object(zd, '_xlsx_rows', return_value=rows):
            h = zd.parse_ssga_navhist(b'x', 'GLD')
            self.assertEqual((len(h), h[-1]), (25, ['2026-09-24', 390.0, 369700000]))
            with self.assertRaises(RuntimeError):
                zd.parse_ssga_navhist(b'x', 'SPY')           # inny symbol w pliku — błąd, nie cudze liczby

    def test_flows_and_groups(self):
        h = [['2026-09-21', 100.0, 1000], ['2026-09-22', 101.0, 1100], ['2026-09-23', 50.0, 2200], ['2026-09-24', 50.0, 2150]]
        self.assertEqual(zd.fund_flows(h), {'2026-09-22': 100 * 101 / 1e6, '2026-09-23': 0.0, '2026-09-24': -50 * 50 / 1e6}, 'podział 2:1 — przepływ dnia liczony po podziale (v93)')
        self.assertEqual(zd.fund_flows([['2026-09-21', 100.0, 1000], ['2026-09-22', 50.0, 1000]]), {}, 'skok NAV bez podziału — dzień pominięty')
        fu = {'SPY': {'h': [['2026-09-22', 10.0, 100], ['2026-09-23', 10.0, 110], ['2026-09-24', 10.0, 130]]},
              'IVV': {'h': [['2026-09-22', 20.0, 50], ['2026-09-23', 20.0, 60]]}}
        ds, v, aum = zd.fund_group(fu, ('SPY', 'IVV'))
        self.assertEqual((ds, aum), (['2026-09-23'], (10.0 * 130 + 20.0 * 60) / 1e6), 'do ostatniego wspólnego dnia')
        self.assertAlmostEqual(v[0], 300 / 1e6)
        fu['IVV']['h'] = [['2026-09-21', 20.0, 40], ['2026-09-23', 20.0, 60]]
        ds, v, _ = zd.fund_group(fu, ('SPY', 'IVV'))
        self.assertEqual(ds, ['2026-09-23']); self.assertAlmostEqual(v[0], 100 / 1e6 + 400 / 1e6, msg='zmiana przez dwa dni przypisana do dnia publikacji')
        self.assertEqual(zd.fund_group(fu, ('SPY', 'EEM')), ([], [], None), 'brak funduszu w grupie — brak grupy, nie część')

    def test_build_fundusze(self):
        zd.META['errors'].clear(); zd.META['notes'].clear()
        ds = self.days(260)
        hist = self.xml([(d.strftime('%b %d, %Y'), 50.0, 1000000 + 100 * i) for i, d in enumerate(ds)][::-1])
        scr = {str(i): {'localExchangeTicker': t, 'portfolioId': str(i), 'navAmount': {'r': 50.0}, 'navAmountAsOf': {'r': 20260925},
                        'totalNetAssetsFund': {'r': 50.0 * 2000000}, 'totalNetAssetsFundAsOf': {'r': 20260925}} for i, t in enumerate(zd.FUND_ISH)}
        calls = []
        def fake(url, timeout=60, headers=None):
            calls.append(url)
            if 'ssga.com' in url:
                raise urllib.error.HTTPError(url, 403, 'Forbidden', None, None)
            if 'product-screener' in url:
                return json.dumps(scr).encode()
            return hist
        now = datetime.datetime(2026, 9, 25, 22, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(zd, 'get_bytes', side_effect=fake), mock.patch.object(zd, '_now_utc', return_value=now), \
                mock.patch.object(zd, 'FUND_SLEEP', 0), mock.patch.object(zd.time, 'sleep'):
            out = zd.build_fundusze(None)
        self.assertEqual(sum('get-fund-document' in u for u in calls), 3, 'najwyżej 3 pełne historie na przebieg')
        full = [t for t, f in out['f'].items() if len(f.get('h') or []) > 1]
        self.assertEqual(len(full), 3); self.assertEqual(out['f'][full[0]]['h'][-1], ['2026-09-25', 50.0, 2000000], 'dzień z zestawienia dopisany')
        self.assertEqual(sum(1 for f in out['f'].values() if f.get('h')), 22, 'pozostałe fundusze iShares — jeden dzień z zestawienia (bez przepływu), do uzupełnienia w kolejnych przebiegach')
        self.assertTrue(any(e.startswith('fundusze ETF: 15 problemów') for e in zd.META['errors']), 'State Street niedostępny — błąd w meta')
        prev = out; prev['f']['SPY'] = {'iss': 'ssga', 'at': (now - datetime.timedelta(hours=1)).isoformat(), 'h': [['2026-09-24', 1.0, 1]]}
        calls.clear()
        with mock.patch.object(zd, 'get_bytes', side_effect=fake), mock.patch.object(zd, '_now_utc', return_value=now), \
                mock.patch.object(zd, 'FUND_SLEEP', 0), mock.patch.object(zd.time, 'sleep'):
            out2 = zd.build_fundusze(prev)
        self.assertFalse(any('spy.xlsx' in u for u in calls), 'plik State Street młodszy niż 6 h — bez pobierania')
        self.assertEqual(sum('get-fund-document' in u for u in calls), 3, 'kolejne 3 fundusze uzupełniane')
        with mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, '_now_utc', return_value=now), \
                mock.patch.object(zd, 'FUND_SLEEP', 0), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_fundusze(None)

    def test_trendy_uses_funds(self):
        ds = self.days(60, '2026-09-18')
        fu = {t: {'h': [[d.isoformat(), 100.0, 1000000 + (5000 if i >= 55 else 1000) * i] for i, d in enumerate(ds)]} for t in ('SPY', 'IVV')}
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone.utc)):
            out = zd.build_trendy({'fundusze': {'f': fu}})
        r = {x['id']: x for x in out['f']}['fe_us']
        self.assertEqual((r['g'], r['m'], r['cur'], r['n'], r['date']), ('fe', 'flow', 'USD', 8, '2026-09-18'))
        self.assertIn(r['st'], ('in_up', 'in_rev', 'in_new'), 'większy napływ w ostatnim tygodniu')
        self.assertTrue(r['ph'] > 0 and r['du'] > 0)
        self.assertEqual([b['id'] for b in out['b']], ['fe'])


class TrendyDailyV120(unittest.TestCase):
    """v120: sygnały dzienne — jedna reguła dla wszystkich rynków, test wstecz na całej historii pliku, brak nie jest zerem, bez sieci."""

    NOW = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.timezone.utc)   # sobota rano (UTC) — wiek danych
    NY = datetime.datetime(2026, 9, 26, 6, 0)                                    # sobota rano w Nowym Jorku (bez strefy, jak zegar w teście)
    STATES = ('buy', 'sell', 'obs', 'x', 'quiet', 'stale', 'short', 'nodata')

    def setUp(self):
        ps = [mock.patch.object(zd, '_now_utc', return_value=self.NOW), mock.patch.object(zd, '_ny_now', return_value=self.NY),
              mock.patch.object(zd.urllib.request, 'urlopen', side_effect=AssertionError('sieć w teście'))]
        for p in ps:
            p.start(); self.addCleanup(p.stop)
        zd.META['notes'].clear()

    @staticmethod
    def days(n, end='2026-09-25'):
        return [d.isoformat() for d in FunduszeV90.days(n, end)]

    @classmethod
    def fund(cls, n=80, end='2026-09-25', last_du=None, last_ret=0.1, iss='ishares'):
        """Historia funduszu: NAV na przemian −0,5 % / +0,5 % (ostatni dzień: last_ret %), jednostki rosną o 1200 / 800 na przemian
        (przepływ ≈ 0,1 mln, wahania poniżej progu dzięki dolnej granicy rozrzutu — zwykły dzień jest „spokojny”); ostatni dzień: zmiana
        jednostek last_du (50 000 = przepływ ≈ 5 mln, wielokrotnie ponad progiem). Seria ±stała leżałaby dokładnie na progu |z| = 1."""
        h, p, u = [], 100.0, 1000000
        for i, d in enumerate(cls.days(n, end)):
            if i:
                p = p * (1 + (last_ret if i == n - 1 else (0.5 if i % 2 else -0.5)) / 100)
                u += (1200 if i % 2 else 800) if not (i == n - 1 and last_du is not None) else last_du
            h.append([d, round(p, 6), u])
        return {'iss': iss, 'h': h}

    @classmethod
    def closes(cls, n=70, last_ret=0.1):
        """Zamknięcia rynku tylko z ceną: +0,4 %, +0,4 %, −0,8 % w kółko (ruch ceny strzela co trzeci dzień); ostatni dzień: last_ret %."""
        out, p = [], 50.0
        for i, d in enumerate(cls.days(n)):
            if i:
                p = p * (1 + (last_ret if i == n - 1 else (-0.8 if i % 3 == 0 else 0.4)) / 100)
            out.append([d, round(p, 4)])
        return out

    def test_z_window(self):
        v = [float(x) for x in range(100)]
        prev = v[10:70]
        self.assertAlmostEqual(zd._td_z(v, 70), (70 - zd._mean(prev)) / max(zd._sd(prev), 0.25 * zd._mean(prev)), places=9, msg='dokładnie 60 poprzednich')
        v[70] = 1000.0
        self.assertAlmostEqual(zd._td_z(v, 70), (1000 - zd._mean(prev)) / zd._sd(prev), places=9, msg='wartość i nie wchodzi do średniej ani rozrzutu')
        v[50] = None
        prev59 = [x for x in v[10:70] if x is not None]
        self.assertAlmostEqual(zd._td_z(v, 70), (1000 - zd._mean(prev59)) / zd._sd(prev59), places=9, msg='None pominięty, nie zero')
        self.assertIsNone(zd._td_z([1.0, 2.0] * 20 + [None] * 21 + [5.0], 61), 'tylko 39 liczb w oknie 60 — za mało')
        self.assertIsNotNone(zd._td_z([1.0, 2.0] * 20 + [None] * 20 + [5.0], 60), '40 liczb — wystarczy')
        self.assertIsNone(zd._td_z(v, 0)); self.assertIsNone(zd._td_z(v, 5)); self.assertIsNone(zd._td_z([None] * 70, 69)); self.assertIsNone(zd._td_z([], 0))
        self.assertEqual(zd._td_z([0.0] * 60 + [1.0], 60), 2.0, 'tło z samych zer, dziś napływ: „dużo większy niż zwykle” (+TD_Z2), nie brak')
        self.assertEqual(zd._td_z([0.0] * 60 + [-0.3], 60), -2.0, 'tło z samych zer, dziś odpływ: −TD_Z2')
        z0 = zd._td_z([0.0] * 60 + [0.0], 60)
        self.assertEqual(json.dumps(z0), '0.0', 'zero po samych zerach: z = 0 (spokojny dzień), nie brak i nie −0')
        self.assertEqual(zd._td_z([0.0] * 30 + [None] * 10 + [0.0] * 20 + [5.0], 60), 2.0, 'None pominięty — 50 zer wystarcza')
        self.assertIsNone(zd._td_z([0.0] * 39 + [None] * 21 + [5.0], 60), 'tylko 39 zer w oknie — za mało historii')
        self.assertIsNone(zd._td_z([0.0] * 60 + [None], 60), 'dziś brak — brak, nie zero')
        self.assertEqual(zd._td_z([7.0] * 60 + [7.0], 60), 0.0, 'stała różna od zera: dolna granica rozrzutu, z = 0')
        self.assertAlmostEqual(zd._td_z([5.0] * 60 + [-5.0], 60), -10 / (0.25 * 5), places=9, msg='stała historia: rozrzut = 1/4 typowego dnia')
        self.assertLessEqual(abs(zd._td_z([5.0] * 60 + [-5.0], 60)), 4 / zd.TD_FLOOR)
        self.assertAlmostEqual(zd._td_z([1.0] * 60 + [1.0], 60, demean=False), 1.0, places=9, msg='zwroty bez odejmowania średniej')
        self.assertAlmostEqual(zd._td_z([0.5, -0.5] * 30 + [1.0], 60, demean=False), 2.0, places=9)
        self.assertIsNone(zd._td_z([0.0] * 60 + [1.0], 60, demean=False))

    def test_split_day(self):
        h = self.fund(80)['h']
        for r in h[50:]:
            r[1], r[2] = r[1] / 2, r[2] * 2                                # podział 2:1 od wiersza 50
        rows = zd._fund_rows(h)
        self.assertEqual(len(rows), 80)
        dates, px, ret = zd._td_fund_px(rows)
        self.assertIsNone(ret[50], 'dzień podziału — zwrot to brak, nie −50 %')
        self.assertAlmostEqual(ret[49], (h[49][1] / h[48][1] - 1) * 100, places=9); self.assertAlmostEqual(ret[51], (h[51][1] / h[50][1] - 1) * 100, places=9)
        self.assertEqual(sum(1 for x in ret if x is None), 2, 'tylko pierwszy wiersz i dzień podziału')
        flow = [1.0 + 0.1 * ((-1) ** i) for i in range(80)]                # spokojne dni (1 ± 0,1: z = ±0,4 dzięki dolnej granicy rozrzutu)
        flow[49] = 50.0                                                     # sygnał w dniu 49 → wynik = zwrot dnia 50 (podział) → para pominięta
        retm = [x if i in (49, 50, 51) else None for i, x in enumerate(ret)]   # cena nie strzela (< 40 liczb), zwroty wokół podziału prawdziwe
        self.assertEqual(zd._td_pairs(dates, flow, retm, 0), [], 'para przez dzień podziału pominięta')
        flow[49], flow[48] = 1.1, 50.0
        self.assertEqual(zd._td_pairs(dates, flow, retm, 0), [(dates[48], 'f', 1, 1)], 'dzień przed: wynik = zwrot dnia 49 (+0,5 %), para jest')

    def test_pairs_outcome(self):
        ds = self.days(50)
        flow = [1.0 + 0.1 * ((-1) ** i) for i in range(50)]                # 1 ± 0,1: zwykły dzień z = ±0,4 (spokojny)
        flow[45] = 100.0
        ret = [None] * 50                                                   # cena: za mało liczb — nigdy nie strzela
        ret[46] = -1.0; ret[47] = 1.0
        self.assertEqual(zd._td_pairs(ds, flow, ret, 0), [(ds[45], 'f', 1, 0)], 'pub 0: wynik z sesji t+1 (pudło)')
        self.assertEqual(zd._td_pairs(ds, flow, ret, 1), [(ds[45], 'f', 1, 1)], 'pub 1: sygnał znany w sesji t+1, wynik z t+2 (trafienie)')
        ds3 = ds[:46] + [(datetime.date.fromisoformat(x) + datetime.timedelta(days=7)).isoformat() for x in ds[46:]]
        self.assertEqual(zd._td_pairs(ds3, flow, ret, 1), [], 'pub 1: luka 8 dni między dniem sygnału a sesją publikacji — para pominięta')
        flow[45] = -100.0
        self.assertEqual(zd._td_pairs(ds, flow, ret, 0), [(ds[45], 'f', -1, 1)], 'kierunek −1: spadek = trafienie')
        ret[46] = 0.0
        self.assertEqual(zd._td_pairs(ds, flow, ret, 0), [], 'dzień bez zmiany ceny — ani trafienie, ani pudło')
        ret[46] = -1.0
        ds2 = list(ds); ds2[46] = (datetime.date.fromisoformat(ds[45]) + datetime.timedelta(days=6)).isoformat()
        self.assertEqual(zd._td_pairs(ds2, flow, ret, 0), [], 'przerwa 6 dni — para pominięta')
        fri = self.days(46, '2026-09-18') + ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24']
        self.assertEqual(zd._td_pairs(fri, flow, ret, 0), [(fri[45], 'f', -1, 1)], 'piątek → poniedziałek (3 dni) zostaje')
        self.assertEqual(zd._td_pairs(ds, flow, [None] * 50, 0), [], 'bez cen — bez par, nie zero')
        flow2 = [1.0 + 0.1 * ((-1) ** i) for i in range(50)]; flow2[45] = 100.0
        ret2 = [None] * 5 + [0.5 * ((-1) ** i) for i in range(40)] + [-5.0] + [None] * 4   # 40 liczb przed dniem 45; potem brak
        self.assertEqual(zd._td_pairs(ds, flow2, ret2, 0), [(ds[45], 'x', 0, None)], 'dzień sprzeczny (napływ, cena w dół): zwracany bez trafienia')

    def test_bonds(self):
        ds = [d.isoformat() for d in (datetime.date(2026, 6, 1) + datetime.timedelta(days=i) for i in range(120)) if d.weekday() < 5]
        first = next(i for i in range(60, len(ds)) if ds[i][:7] != ds[i - 1][:7])   # pierwszy wiersz miesiąca po 60 dniach
        self.assertTrue(zd._td_month_first(ds, first)); self.assertFalse(zd._td_month_first(ds, first + 1)); self.assertTrue(zd._td_month_first(ds, 0))
        flow = [1.0 + 0.1 * ((-1) ** i) for i in range(len(ds))]          # spokojne dni
        flow[first - 1] = 50.0; flow[first] = 50.0
        ret = [0.6 if i % 2 else -0.2 for i in range(len(ds))]             # cena strzela co drugi dzień — u obligacji nie wchodzi do reguły
        ret[first] = -2.0; ret[first + 1] = 2.0
        pr = zd._td_pairs(ds, flow, ret, 0, bd=True)
        self.assertEqual(pr, [(ds[first], 'f', 1, 1)], 'obligacje: wynik z pierwszej sesji miesiąca pominięty; cena nie wchodzi do reguły')
        self.assertTrue(all(p[1] == 'p' for p in zd._td_pairs(ds[:first - 1], flow[:first - 1], ret[:first - 1], 0, bd=False)), 'akcje: ta sama seria dałaby regułę p')
        self.assertEqual(zd._td_class(0.2, 3.0, bd=True), ('none', 0, 0), 'obligacje: sama cena nie daje reguły')
        self.assertEqual(zd._td_class(1.5, -3.0, bd=True), ('f', 1, 1), 'obligacje: cena nie robi też dnia sprzecznego')
        self.assertEqual(zd._td_class(2.5, 3.0, bd=True), ('f', 1, 2))
        fu = {'TLT': self.fund(80, last_du=50000, last_ret=3.0)}
        d, bd = zd.build_daily({'fundusze': {'f': fu}})
        r = d[0]
        self.assertEqual((r['id'], r['fam'], r['rule'], r['dir'], r['pub']), ('TLT', 'bd', 'f', 1, 0))
        self.assertTrue(zd._isnum(r['zp']) and r['zp'] > 1, 'zp policzone i pokazane, choć nie wchodzi do reguły')

    def test_class_table(self):
        T = {(-1.5, -1.5): ('fp', -1, 2), (-1.5, 0.0): ('f', -1, 1), (-1.5, 1.5): ('x', 0, 0),
             (0.0, -1.5): ('p', -1, 1), (0.0, 0.0): ('none', 0, 0), (0.0, 1.5): ('p', 1, 1),
             (1.5, -1.5): ('x', 0, 0), (1.5, 0.0): ('f', 1, 1), (1.5, 1.5): ('fp', 1, 2)}
        for (zf, zp), exp in T.items():
            self.assertEqual(zd._td_class(zf, zp), exp, (zf, zp))
        self.assertEqual(zd._td_class(None, None), ('none', 0, 0)); self.assertEqual(zd._td_class(None, 1.2), ('p', 1, 1)); self.assertEqual(zd._td_class(-2.0, None), ('f', -1, 2))
        self.assertEqual(zd._td_class(2.5, 1.5), ('fp', 1, 3), 'siła najwyżej 3'); self.assertEqual(zd._td_class(2.5, 2.5), ('fp', 1, 3))
        self.assertEqual(zd._td_class(0.99, 0.99), ('none', 0, 0), 'próg 1 rozrzutu'); self.assertEqual(zd._td_class(1.0, 0.0), ('f', 1, 1))
        self.assertEqual(zd._td_class(0.5, 2.0), ('p', 1, 2)); self.assertEqual(zd._td_class(-0.5, -1.0), ('p', -1, 1))

    def test_pool(self):
        def dates(n, start='2026-01-05'):
            d0, out = datetime.date.fromisoformat(start), []
            while len(out) < n:
                if d0.weekday() < 5:
                    out.append(d0.isoformat())
                d0 += datetime.timedelta(days=1)
            return out
        ds = dates(120)
        lines = {('eq', 'f'): [(d, 1 if i % 3 else 0, 'A') for i, d in enumerate(ds)] + [(d, 1 if i % 3 else 0, 'B') for i, d in enumerate(ds)]}
        with mock.patch.object(zd, 'wilson', wraps=zd.wilson) as w:
            bd = zd._td_pool(lines)
        self.assertEqual(len(bd), 7); self.assertEqual([(b['fam'], b['rule']) for b in bd], list(zd.TD_RULES))
        e = bd[0]
        self.assertEqual((e['k'], e['n'], e['days'], e['m'], e['from'], e['to'], e['need']), (160, 240, 120, 2, ds[0], ds[-1], 0), 'dwa rynki na tych samych dniach: dni < par')
        w.assert_any_call(160, 240, n_eff=120)
        self.assertEqual(e['p'], 66.7); self.assertEqual(e['ci'], list(zd.wilson(160, 240, n_eff=120))); self.assertEqual(e['vd'], 'edge')
        self.assertTrue(e['h1'] > 50 and e['h2'] > 50); self.assertEqual((e['lk'], e['ln'], e['ldays']), (0, 0, 0), 'przed wdrożeniem — licznik pusty')
        for b in bd[1:]:
            self.assertEqual((b['k'], b['n'], b['days'], b['p'], b['ci'], b['h1'], b['h2'], b['m'], b['need'], b['vd'], b['from']),
                             (0, 0, 0, None, [None, None], None, None, 0, 100, 'short', None), 'linia bez par: braki, nie zera')
        anti = zd._td_pool({('pm', 'p'): [(d, 1 if i % 3 == 0 else 0, 'SLV') for i, d in enumerate(ds)]})[5]
        self.assertEqual((anti['fam'], anti['rule'], anti['vd'], anti['p']), ('pm', 'p', 'anti', 33.3)); self.assertTrue(anti['ci'][1] < 50)
        veto = zd._td_pool({('eq', 'p'): [(d, 1 if i < 60 or i % 20 < 9 else 0, 'A') for i, d in enumerate(ds)]})[1]   # 60/60, potem 27/60
        self.assertTrue(veto['ci'][0] > 50 and veto['h1'] > 50 and veto['h2'] < 50, veto)
        self.assertEqual(veto['vd'], 'none', 'druga połowa poniżej 50 — bez przewagi mimo dolnej granicy > 50')
        none = zd._td_pool({('eq', 'fp'): [(d, i % 2, 'A') for i, d in enumerate(ds)]})[2]
        self.assertEqual((none['vd'], none['p']), ('none', 50.0))
        short = zd._td_pool({('pm', 'f'): [(d, 1, 'GLD') for d in ds[:70]]})[4]
        self.assertEqual((short['vd'], short['days'], short['need'], short['p']), ('short', 70, 30, 100.0), 'za mało dni mimo 100 % trafień')
        late = dates(30, '2026-09-28')
        oos = zd._td_pool({('eq', 'f'): [(d, 1, 'A') for d in ds] + [(d, i % 2, 'A') for i, d in enumerate(late)] + [(late[0], 1, 'B')]})[0]
        self.assertEqual((oos['lk'], oos['ln'], oos['ldays']), (16, 31, 30), 'od wdrożenia: tylko daty ≥ TD_SINCE')
        self.assertEqual(zd._td_pool({})[3]['vd'], 'short'); self.assertEqual(zd._td_pool(None)[6]['rule'], 'fp')

    def test_live_clock(self):
        sat = datetime.datetime(2026, 9, 26, 6, 0)
        self.assertTrue(zd._td_live('2026-09-25', sat), 'dane z piątku, sobota — aktualne do poniedziałku 16:15')
        self.assertEqual(zd._td_next('2026-09-25').isoformat(), '2026-09-28', 'następna sesja po piątku = poniedziałek')
        self.assertFalse(zd._td_live('2026-09-24', sat), 'dane z czwartku, sobota — sesja piątkowa minęła')
        self.assertFalse(zd._td_live('2026-09-25', datetime.datetime(2026, 9, 28, 16, 20)), 'poniedziałek 16:20 NY — nieaktualne')
        self.assertTrue(zd._td_live('2026-09-25', datetime.datetime(2026, 9, 28, 15, 0)), 'poniedziałek 15:00 NY — aktualne')
        self.assertTrue(zd._td_live('2026-09-25', datetime.datetime(2026, 9, 28, 16, 14, 59)))
        self.assertFalse(zd._td_live('2026-09-25', datetime.datetime(2026, 9, 28, 16, 15)))
        from zoneinfo import ZoneInfo
        self.assertFalse(zd._td_live('2026-09-25', datetime.datetime(2026, 9, 28, 16, 20, tzinfo=ZoneInfo('America/New_York'))), 'zegar ze strefą — to samo')
        self.assertEqual(zd._td_next('2026-09-26').isoformat(), '2026-09-28'); self.assertEqual(zd._td_next('2026-09-23').isoformat(), '2026-09-24')

    def test_country_rows(self):
        ds = self.days(80)
        ewt = self.fund(80); ewt['h'] = ewt['h'][:-1]                       # ostatni dzień przepływu bez NAV
        tw = {'d': [[d, 100.0 + 20.0 * ((-1) ** i), 0, 0, 0, 3.1 + 0.6 * ((-1) ** i), d] for i, d in enumerate(ds)], 'empty': []}   # spokojne dni
        tw['d'][-1][1] = 5000.0; tw['d'][-1][5] = 155.0
        india = {'d': [[d, 20.0 + 4.0 * ((-1) ** i), 1.0, 0, 0, 88.0] for i, d in enumerate(ds)]}
        india['d'][-1][1] = 900.0
        S = {'obce': {'tw': tw, 'in': india}, 'fundusze': {'f': {'EWT': ewt, 'INDA': self.fund(80)}}}
        d, _ = zd.build_daily(S)
        by = {r['id']: r for r in d}
        self.assertEqual(set(by), {'EWT', 'INDA', 'in_eq', 'tw'})
        t = by['tw']
        self.assertEqual((t['sym'], t['cur'], t['f'], t['fu'], t['grp'], t['iss'], t['pub'], t['date']), ('EWT', 'TWD', 5000.0, 155.0, None, None, 0, ds[-1]))
        self.assertIsNone(t['r']); self.assertIsNone(t['zp']); self.assertNotEqual(t['r'], 0)
        self.assertEqual((t['rule'], t['dir'], t['st'], t['side']), ('f', 1, 'obs', 'buy'), 'sam przepływ (cena: brak) daje regułę f')
        i = by['in_eq']
        self.assertEqual((i['sym'], i['cur'], i['f'], i['fu'], i['rule']), ('INDA', 'USD', 900.0, 900.0, 'f'), 'Indie już w USD: fu = f')
        self.assertTrue(zd._isnum(i['r']) and zd._isnum(i['zp']), 'NAV z tego dnia jest — zwrot policzony')
        d2, b2 = zd.build_daily({'obce': {'tw': tw, 'in': india}})
        self.assertEqual((d2, b2), (None, None), 'bez pliku funduszy — bez wierszy krajów (nie karta z samymi brakami); bez żadnej serii — blok niepoliczony')
        s = zd._td_series_ob({'tw': tw}, zd.TD_OB[1], {'EWT': self.fund(80)})
        self.assertEqual(len(s['dates']), 80); self.assertIsNone(s['ret'][0]); self.assertTrue(all(zd._isnum(x) for x in s['ret'][1:]))
        holes = dict(tw); holes['d'] = tw['d'][:-2] + tw['d'][-1:]         # dzień roboczy bez wiersza → kalendarz sesji: brak
        s = zd._td_series_ob({'tw': holes}, zd.TD_OB[1], {'EWT': self.fund(80)})
        self.assertEqual(len(s['dates']), 80); self.assertIsNone(s['flow'][-2]); self.assertIsNotNone(s['ret'][-2], 'NAV jest, więc zwrot jest')

    def test_build_trendy_integration(self):
        import copy
        S = TrendyV89('test_build_rows').S(); S0 = copy.deepcopy(S)
        out = zd.build_trendy(S)
        with mock.patch.object(zd, 'build_daily', return_value=([], [])):
            ref = zd.build_trendy(S)
        self.assertEqual(S, S0, 'build_trendy nie zmienia wejścia')
        for k in ('f', 'p', 'b', 'rules', 'v'):
            self.assertEqual(out[k], ref[k], f'{k}: część tygodniowa bajt w bajt jak przed łatką')
        self.assertEqual(set(out) - set(ref), set()); self.assertEqual(set(out), {'at', 'v', 'src', 'rules', 'f', 'p', 'b', 'dv', 'dsince', 'dr', 'd', 'bd'})
        self.assertEqual((out['dv'], out['dsince']), (1, '2026-09-28'))
        self.assertEqual(out['dr'], {'lb': 60, 'min': 40, 'z1': 1.0, 'z2': 2.0, 'floor': 0.25, 'gap': 4, 'neff': 100, 'pub': {'ishares': 0, 'ssga': 1},
                                     'pubsym': {'GLD': 0, 'GLDM': 0}, 'excluded': ['br', 'mx', 'th', 'in_bd', 'jp', 'tr', 'cf', 'cs', 'cr', 'ix']})
        self.assertEqual((out['d'], out['bd']), (None, None), 'bez funduszy, krajów i cen — blok niepoliczony (null), nie „zero rynków”')
        self.assertIn('"d": null, "bd": null', json.dumps(out, ensure_ascii=False))
        self.assertEqual([b['id'] for b in out['b']], ['th', 'mx', 'ob'], 'część tygodniowa nietknięta')
        px = self.closes(70)
        out = zd.build_trendy({'ceny': {'q': {'EWC': {'d': px}, 'KSA': {'d': px[:10]}}}})
        self.assertEqual([r['id'] for r in out['d']], ['EWC', 'KSA'], 'bez funduszy: tylko rynki z ceną')
        e = out['d'][0]
        self.assertEqual((e['f'], e['zf'], e['fu'], e['cur'], e['grp'], e['iss'], e['pub']), (None, None, None, None, None, None, 0), 'rynek tylko z ceną: przepływ = brak')
        self.assertTrue(zd._isnum(e['r']) and zd._isnum(e['zp'])); self.assertEqual(out['d'][1]['st'], 'short', '10 dni cen — za mało historii')
        self.assertEqual([(b['fam'], b['rule']) for b in out['bd']], list(zd.TD_RULES), 'same ceny: 7 linii, jest co liczyć')
        self.assertTrue(all(b['n'] == 0 and b['vd'] == 'short' for b in out['bd'] if b['fam'] != 'eq' or b['rule'] != 'p'))
        zd.META['notes'].clear()
        out = zd.build_trendy({'fundusze': {'f': {'IVV': self.fund(80)}}, 'ceny': {'q': {'EWC': {'d': 5}}}})
        self.assertEqual([r['id'] for r in out['d']], ['IVV'], 'zepsuta seria cen — wiersz funduszu zostaje')
        self.assertTrue(any(n.startswith('trendy dziennie EWC:') for n in zd.META['notes']), zd.META['notes'])
        zd.META['notes'].clear()
        S2 = {'fundusze': {'f': {'IVV': self.fund(80)}}}
        ok = zd.build_trendy(S2)
        self.assertEqual([r['id'] for r in ok['d']], ['IVV']); zd.META['notes'].clear()
        with mock.patch.object(zd, 'build_daily', side_effect=RuntimeError('awaria')):
            out = zd.build_trendy(S2)
        self.assertEqual((out['d'], out['bd'], out['dv']), (None, None, 1), 'awaria to nie „zero rynków”: null, strona ukrywa blok')
        self.assertIn('trendy dziennie: awaria', zd.META['notes'])
        for k in ('f', 'p', 'b', 'rules', 'v'):
            self.assertEqual(out[k], ok[k], f'{k}: część tygodniowa policzona jak zwykle')
        self.assertIsNone(zd.build_trendy(None)['d'])

    def test_constants(self):
        import inspect
        self.assertIsInstance(zd.TD_Z1, float); self.assertIsInstance(zd.TD_Z2, float); self.assertIsInstance(zd.TD_FLOOR, float)
        self.assertEqual(len(zd.TD_RULES), 7); self.assertEqual(len(set(zd.TD_RULES)), 7)
        self.assertEqual({f for f, _ in zd.TD_RULES}, {'eq', 'bd', 'pm'}); self.assertNotIn(('bd', 'p'), zd.TD_RULES); self.assertNotIn(('bd', 'fp'), zd.TD_RULES)
        src = inspect.getsource(zd._td_class) + inspect.getsource(zd._td_z) + inspect.getsource(zd._td_pairs) + inspect.getsource(zd._td_pool)
        for sym in list(zd.TD_GRP) + list(zd.TD_PX) + ['in_eq', 'tw', 'hk']:
            self.assertNotRegex(src, r"'" + sym + r"'", f'żadnych progów na rynek: {sym}')
        self.assertFalse(set(zd.TD_FAM['bd']) & set(zd.TD_FAM['pm']))
        self.assertTrue(set(zd.TD_FAM['bd']) | set(zd.TD_FAM['pm']) <= set(zd.TD_GRP), 'rodziny tylko z funduszy z TR_FE')
        self.assertEqual(len(zd.TD_GRP), 37); self.assertEqual(zd.TD_GRP['SPY'], 'fe_us'); self.assertEqual(zd.TD_GRP['SLV'], 'fe_silver')
        self.assertEqual(zd.TD_SINCE, '2026-09-28'); self.assertEqual(zd.TD_V, 1); self.assertEqual(zd.TD_PUB, {'ishares': 0, 'ssga': 1})
        self.assertEqual(zd.TD_PUB_SYM, {'GLD': 0, 'GLDM': 0}); self.assertTrue(set(zd.TD_PUB_SYM) <= set(zd.TD_GRP), 'wyjątki tylko dla funduszy z listy')
        self.assertEqual({t: zd._td_series_fund({t: self.fund(80, iss=i)}, t)['pub'] for t, i in (('GLD', 'ssga'), ('GLDM', 'ssga'), ('SPY', 'ssga'), ('BIL', 'ssga'), ('IAU', 'ishares'))},
                         {'GLD': 0, 'GLDM': 0, 'SPY': 1, 'BIL': 1, 'IAU': 0}, 'wyjątek pliku przed wydawcą; pozostałe pliki wydawcy bez zmian')
        self.assertEqual([o[0] for o in zd.TD_OB], ['in_eq', 'tw', 'hk']); self.assertEqual(len(zd.TD_PX), 9)
        self.assertNotIn('SPY', zd.TD_PX, 'SPY ma przepływy — nie jest rynkiem tylko z ceną')

    def test_rows_nulls_and_states(self):
        fu = {'IVV': self.fund(80, last_du=50000), 'SPY': self.fund(80, end='2026-09-24', iss='ssga'), 'GLD': self.fund(80, last_ret=-3.0, iss='ssga'),
              'SLV': self.fund(30), 'XLK': self.fund(80, last_du=50000, last_ret=-3.0), 'EEM': self.fund(80), 'EWT': self.fund(80, end='2026-09-24')}
        px = self.closes(70)
        tw = {'d': [[d, 100.0 + 20.0 * ((-1) ** i), 0, 0, 0, 3.1 + 0.6 * ((-1) ** i), d] for i, d in enumerate(self.days(80))], 'empty': []}
        tw['d'][-1][1] = None; tw['d'][-1][5] = None                        # ostatni dzień bez liczb (a NAV EWT z tego dnia też brak)
        d, bd = zd.build_daily({'fundusze': {'f': fu}, 'ceny': {'q': {'EWC': {'d': px}}}, 'obce': {'tw': tw}})
        by = {r['id']: r for r in d}
        self.assertEqual([r['id'] for r in d], ['SPY', 'IVV', 'XLK', 'EEM', 'EWT', 'GLD', 'SLV', 'tw', 'EWC'], 'kolejność: fundusze wg TR_FE, kraje, ceny')
        for r in d:
            self.assertIn(r['st'], self.STATES); self.assertIn(r['rule'], ('f', 'p', 'fp', 'x', 'none')); self.assertIn(r['dir'], (-1, 0, 1))
            self.assertIn(r['str'], (0, 1, 2, 3)); self.assertIn(r['vd'], ('edge', 'anti', 'none', 'short', None))
            for k in ('f', 'zf', 'fu', 'r', 'zp'):
                self.assertTrue(r[k] is None or (zd._isnum(r[k]) and not isinstance(r[k], bool)), (r['id'], k, r[k]))
            self.assertEqual(r['side'], ('buy' if r['dir'] > 0 else 'sell') if r['st'] in ('buy', 'sell', 'obs') else 'none', r['id'])
            if r['st'] in ('buy', 'sell', 'obs'):
                self.assertNotEqual(r['dir'], 0); self.assertTrue(r['live'])
            self.assertEqual(r['nx'] > r['date'], True); self.assertEqual(len(r), 24)
        self.assertEqual((by['IVV']['st'], by['IVV']['rule'], by['IVV']['side'], by['IVV']['str'], by['IVV']['vd']), ('obs', 'f', 'buy', 2, 'short'))
        self.assertEqual((by['IVV']['ik'], by['IVV']['in']), (0, 0), 'ten rynek: jeszcze bez par — zero par to prawdziwe zero')
        self.assertEqual((by['SPY']['st'], by['SPY']['pub'], by['SPY']['date'], by['SPY']['nx'], by['SPY']['live'], by['SPY']['age'], by['SPY']['side']),
                         ('stale', 1, '2026-09-24', '2026-09-25', False, 2, 'none'), 'wiersz z czwartku w sobotę — nieaktualny')
        self.assertEqual((by['GLD']['st'], by['GLD']['rule'], by['GLD']['dir'], by['GLD']['side'], by['GLD']['fam']), ('obs', 'p', -1, 'sell', 'pm'))
        self.assertEqual((by['GLD']['iss'], by['GLD']['pub']), ('ssga', 0), 'trust złota: dane dzień wcześniej niż reszta plików wydawcy')
        self.assertEqual((by['SLV']['st'], by['SLV']['zf'], by['SLV']['zp']), ('short', None, None), '30 dni — za mało historii, z = brak')
        self.assertEqual((by['XLK']['st'], by['XLK']['rule'], by['XLK']['dir'], by['XLK']['side'], by['XLK']['str']), ('x', 'x', 0, 'none', 0), 'napływ, ale cena w dół')
        self.assertEqual((by['EEM']['st'], by['EEM']['rule']), ('quiet', 'none'))
        self.assertEqual((by['tw']['st'], by['tw']['f'], by['tw']['fu'], by['tw']['zf']), ('nodata', None, None, None), 'brak liczb z tego dnia — brak, nie zero')
        self.assertEqual((by['EWC']['st'], by['EWC']['f'], by['EWC']['ik'], by['EWC']['in']), ('quiet', None, None, None))
        self.assertEqual(len(bd), 7); self.assertTrue(all(b['vd'] == 'short' for b in bd), 'krótkie serie — wszystkie linie „za mało historii”')
        self.assertEqual((bd[0]['n'], bd[0]['m'], bd[0]['p']), (0, 0, None), 'spokojne przepływy w historii — linia eq·f bez par (brak, nie zero)')
        self.assertTrue(bd[1]['n'] > 0 and bd[1]['m'] >= 1 and bd[1]['days'] < bd[1]['n'], 'eq·p: kilka funduszy na tych samych dniach — dni < par')

    def test_flat_flow_history(self):
        """Przegląd v120, uwaga 1: liczba jednostek bez zmian przez tygodnie (przepływ dokładnie 0 każdego dnia), potem tworzenie jednostek.
        Prawdziwa liczba nie może wyjść jako brak: karta nie może pisać „brak danych”, a test wstecz musi widzieć taki dzień."""
        def hist(last_du):
            h, p = [], 100.0
            for i, d in enumerate(self.days(80)):
                if i:
                    p = p * (1 + (0.1 if i == 79 else (0.5 if i % 2 else -0.5)) / 100)
                h.append([d, round(p, 6), 1000000 + (last_du if i == 79 else 0)])
            return {'iss': 'ishares', 'h': h}
        d, bd = zd.build_daily({'fundusze': {'f': {'EFA': hist(50000), 'EEM': hist(0), 'EWJ': hist(-50000)}}})
        by = {r['id']: r for r in d}
        e = by['EFA']
        self.assertTrue(e['f'] > 4.9, e['f'])
        self.assertEqual((e['zf'], e['rule'], e['dir'], e['str'], e['st'], e['side']), (2.0, 'f', 1, 2, 'obs', 'buy'), 'duży napływ po płaskim tle — sygnał „dużo większy”')
        self.assertEqual((by['EWJ']['zf'], by['EWJ']['rule'], by['EWJ']['side']), (-2.0, 'f', 'sell'), 'odpływ po płaskim tle')
        q = by['EEM']
        self.assertEqual((q['f'], q['zf'], q['rule'], q['st']), (0.0, 0.0, 'none', 'quiet'), 'zero po zerach: prawdziwe 0 i z = 0 (spokojny dzień), nie brak')
        for r in d:
            self.assertFalse(zd._isnum(r['f']) and r['zf'] is None, 'liczba przepływu przy 40+ dniach tła zawsze ma z')
        ds = self.days(50); flow = [0.0] * 50; flow[45] = 12.5
        ret = [None] * 50; ret[46] = 0.7
        self.assertEqual(zd._td_pairs(ds, flow, ret, 0), [(ds[45], 'f', 1, 1)], 'napływ po samych zerach — para f w teście wstecz')

    def test_failure_isolated(self):
        """Przegląd v120, uwaga 2: awaria jednego rynku (test wstecz albo karta) nie kasuje pozostałych; brak kart = None, nie []."""
        fu = {'IVV': self.fund(80), 'EEM': self.fund(80), 'EWJ': self.fund(80)}
        real_pairs, real_row = zd._td_pairs, zd._td_row
        calls = []

        def pairs(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('zła seria')
            return real_pairs(*a, **k)

        def row(s, *a):
            if s['id'] == 'EWJ':
                raise ValueError('zły dzień')
            return real_row(s, *a)
        with mock.patch.object(zd, '_td_pairs', side_effect=pairs), mock.patch.object(zd, '_td_row', side_effect=row):
            d, bd = zd.build_daily({'fundusze': {'f': fu}})
        self.assertEqual([r['id'] for r in d], ['EEM'], 'IVV (test wstecz) i EWJ (karta) bez kart — EEM zostaje')
        self.assertEqual(len(bd), 7)
        self.assertEqual([n for n in zd.META['notes'] if n.startswith('trendy dziennie')], ['trendy dziennie IVV: zła seria', 'trendy dziennie EWJ: zły dzień'])
        zd.META['notes'].clear()
        with mock.patch.object(zd, '_td_row', side_effect=RuntimeError('wszystko')):
            self.assertEqual(zd.build_daily({'fundusze': {'f': fu}}), (None, None), 'żadnej karty — blok niepoliczony, nie „zero rynków”')
        self.assertEqual(len(zd.META['notes']), 3)
        self.assertEqual(zd.build_daily({}), (None, None)); self.assertEqual(zd.build_daily(None), (None, None))
        self.assertEqual(zd.build_daily({'fundusze': {'f': {'IVV': {'iss': 'ishares', 'h': [['2026-09-25', 1.0, 1]]}}}}), (None, None), 'jeden wiersz — bez serii')

    def test_country_price_not_pooled(self):
        """Przegląd v120, uwaga 4: wiersz kraju ma cenę z NAV funduszu, który ma własny wiersz — jego pary „p” nie idą do linii
        zbiorczej (ten sam dzień i ta sama sesja wyniku liczyłyby się dwa razy); pary „f”/„fp” (własne wejście: przepływ zagraniczny) idą."""
        ds = self.days(80)
        india = {'d': [[x, 20.0 + 4.0 * ((-1) ** i), 1.0, 0, 0, 88.0] for i, x in enumerate(ds)]}
        fixed = [(ds[50], 'p', 1, 1), (ds[51], 'f', 1, 0), (ds[52], 'fp', -1, 1), (ds[53], 'x', 0, None)]
        with mock.patch.object(zd, '_td_pairs', return_value=fixed):
            d, bd = zd.build_daily({'obce': {'in': india}, 'fundusze': {'f': {'INDA': self.fund(80)}}})
        self.assertEqual([r['id'] for r in d], ['INDA', 'in_eq'])
        line = {(b['fam'], b['rule']): b for b in bd}
        self.assertEqual((line[('eq', 'p')]['n'], line[('eq', 'p')]['m']), (1, 1), 'eq·p: tylko para funduszu, bez duplikatu z wiersza kraju')
        self.assertEqual((line[('eq', 'f')]['n'], line[('eq', 'f')]['m'], line[('eq', 'fp')]['n'], line[('eq', 'fp')]['m']), (2, 2, 2, 2), 'f i fp z obu wierszy')
        s = zd._td_series_ob({'in': india}, zd.TD_OB[0], {'INDA': self.fund(80)})
        self.assertEqual(s['nopool'], ('p',)); self.assertNotIn('nopool', zd._td_series_fund({'INDA': self.fund(80)}, 'INDA'))
        self.assertNotIn('nopool', d[1], 'klucz wewnętrzny — nie trafia do pliku')

    def test_anti_never_flips_side(self):
        fu = {'IVV': self.fund(80, last_du=50000), 'GLD': self.fund(80, last_du=-50000, iss='ishares')}
        base = zd._td_pool({})
        for vd in ('anti', 'none', 'short', 'edge'):
            fake = [dict(b, vd=vd) for b in base]
            with mock.patch.object(zd, '_td_pool', return_value=fake):
                d, bd = zd.build_daily({'fundusze': {'f': fu}})
            by = {r['id']: r for r in d}
            exp = ('buy', 'sell') if vd == 'edge' else ('obs', 'obs')
            self.assertEqual((by['IVV']['st'], by['IVV']['side'], by['IVV']['dir'], by['IVV']['vd']), (exp[0], 'buy', 1, vd), vd)
            self.assertEqual((by['GLD']['st'], by['GLD']['side'], by['GLD']['dir'], by['GLD']['vd']), (exp[1], 'sell', -1, vd), 'anti/none/short: obserwacja po stronie kierunku, nigdy odwrotnie')
            self.assertEqual([b['vd'] for b in bd], [vd] * 7)


class ObceV91(unittest.TestCase):
    """v91: Indie, Tajwan, Hongkong co godzinę; Brazylia, Turcja, ThaiBMA najwyżej co 3 h (bez zapytania, gdy część świeża i bez błędu)."""

    def test_slow_parts_reused(self):
        called = []
        mk = lambda name: (lambda *a, **k: called.append(name) or {'at': zd.NOW, 'd': [['2026-09-24', 1.0]]})
        prev = {'ok': {'in': True, 'tw': True, 'hk': True, 'br': True, 'tr': False, 'th': True},
                'br': {'at': _iso(30), 'd': [['2026-09-18', 2.0]]}, 'tr': {'at': _iso(30), 'd': []}, 'th': {'at': _iso(200), 'd': []}}
        with mock.patch.object(zd, 'nsdl_part', mk('in')), mock.patch.object(zd, 'twse_part', mk('tw')), mock.patch.object(zd, 'hkex_part', mk('hk')), \
                mock.patch.object(zd, 'bcb_part', mk('br')), mock.patch.object(zd, 'tcmb_part', mk('tr')), mock.patch.object(zd, 'thbma_part', mk('th')):
            out = zd.build_obce('', prev)
        self.assertEqual(called, ['in', 'tw', 'hk', 'tr', 'th'], 'Brazylia świeża (30 min) — z pamięci; Turcja z błędem i ThaiBMA sprzed 200 min — pobrane')
        self.assertIs(out['br'], prev['br']); self.assertIs(out['ok']['br'], True); self.assertEqual(zd.META['ok']['obce_br'], 'cached')


class SurowceV92(unittest.TestCase):
    """v92: CFTC disaggregated — złoto, srebro, miedź, ropa WTI; pozycje grup muszą sumować się do open interest."""

    @staticmethod
    def row(code, day, oi, mm=(100, 20, 10), bad=False):
        vals = {'Market_and_Exchange_Names': 'GOLD - COMMODITY EXCHANGE INC.', 'As_of_Date_In_Form_YYMMDD': day[2:].replace('-', ''),
                'Report_Date_as_YYYY-MM-DD': day, 'CFTC_Contract_Market_Code': code, 'CFTC_Market_Code': 'CMX', 'CFTC_Region_Code': '0',
                'CFTC_Commodity_Code': '88', 'Open_Interest_All': oi}
        L = {'Prod_Merc_Positions_Long_All': 50, 'Prod_Merc_Positions_Short_All': 150, 'Swap_Positions_Long_All': 30, 'Swap__Positions_Short_All': 20,
             'Swap__Positions_Spread_All': 5, 'M_Money_Positions_Long_All': mm[0], 'M_Money_Positions_Short_All': mm[1], 'M_Money_Positions_Spread_All': mm[2],
             'Other_Rept_Positions_Long_All': 10, 'Other_Rept_Positions_Short_All': 10, 'Other_Rept_Positions_Spread_All': 0,
             'NonRept_Positions_Long_All': 0, 'NonRept_Positions_Short_All': 0, 'Tot_Rept_Positions_Long_All': 0, 'Tot_Rept_Positions_Short_All': 0}
        vals.update(L)
        # NonRept dobrane tak, by obie strony (long + spread, short + spread) dały open interest; bad=True psuje sumę shortów
        vals['NonRept_Positions_Long_All'] = oi - (50 + 30 + 5 + mm[0] + mm[2] + 10)
        vals['NonRept_Positions_Short_All'] = oi - (150 + 20 + 5 + mm[1] + mm[2] + 10) + (7 if bad else 0)
        return [str(vals[c]) for c in zd.CFTCD_COLS] + ['x'] * 168

    def text(self, rows, header=True):
        import csv as _csv, io
        buf = io.StringIO(); w = _csv.writer(buf)
        if header:
            w.writerow(list(zd.CFTCD_COLS) + ['Extra'] * 168)
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def test_parse_and_build(self):
        import io, zipfile
        days = [(datetime.date(2026, 6, 16) + datetime.timedelta(days=7 * i)).isoformat() for i in range(14)]
        year = [self.row('088691', d, 1000, mm=(100 + 10 * i, 20, 10)) for i, d in enumerate(days[:-1])] + [self.row('999999', days[0], 5)]
        year.append(self.row('088691', days[3], 1000, bad=True))            # zła suma — wiersz pominięty; poprawny wiersz z tego dnia zostaje
        week = [self.row('088691', days[-1], 1000, mm=(300, 20, 10))]
        p, bad = zd.parse_cftcd(self.text(year))
        self.assertEqual(sorted(p), ['088691'], 'tylko rynki z listy'); self.assertEqual(bad, ['088691 ' + days[3]])
        self.assertEqual(p['088691'][days[0]]['g']['mm'], {'long': 100, 'short': 20, 'spread': 10, 'net': 80})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('f_year.txt', self.text(year))
        files = {zd.CFTCD_YEAR_URL.format(2026): buf.getvalue(), zd.CFTCD_WEEK_URL: self.text(week, header=False).encode()}
        zd.META['notes'].clear(); zd.META['errors'].clear()
        out = zd.build_surowce(fetch=lambda u: files[u] if u in files else (_ for _ in ()).throw(RuntimeError('404')), today=datetime.date(2026, 9, 25))
        g = out['markets']['gold']
        self.assertEqual((g['asof'], g['groups']['mm']['net'], len(g['hist']['dates']), g['hist']['mm'][-2:]), (days[-1], 280, 13, [200, 280]),
                         'tydzień z pliku tygodniowego; 13 raportów z okna 90 dni')
        self.assertTrue(any('pozycje ≠ open interest' in n for n in zd.META['notes']))
        self.assertTrue(any('brak rynku silver' in e for e in zd.META['errors']), 'brak rynku — błąd, nie zero')
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)):
            r = {x['id']: x for x in zd.build_trendy({'surowce': out})['f']}['cs_gold']
        self.assertEqual((r['g'], r['m'], r['cur'], r['w'], r['date']), ('pos', 'pos', 'CT', 80, days[-1]))


class CenyFunduszyV93(unittest.TestCase):
    """v93: TRENDY — tydzień ceny jednostki (NAV) największego funduszu w grupie: obligacje, metale, sektory, regiony spoza listy cen krajów."""

    def test_nav_rows(self):
        days = [d for d in (datetime.date(2025, 9, 1) + datetime.timedelta(days=i) for i in range(390)) if d.weekday() < 5][-280:]
        mk = lambda nav0, sh, step: [[d.isoformat(), nav0 + step * i, sh] for i, d in enumerate(days)]
        fu = {'GLD': {'h': mk(300.0, 400, 0.1)}, 'IAU': {'h': mk(60.0, 700, 0.02)}, 'GLDM': {'h': mk(70.0, 10, 0.02)},
              'TLT': {'h': mk(90.0, 500, -0.01)}, 'XLK': {'h': mk(200.0, 600, 0.01)}}
        fu['XLK']['h'][-3][1] = 100.0                                   # skok NAV o połowę — podział jednostek, bez wiersza
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)):
            zd.META['notes'].clear()
            out = zd.build_trendy({'fundusze': {'f': fu}})
        by = {r['id']: r for r in out['p']}
        self.assertEqual((by['fp_gold']['sym'], by['fp_gold']['g']), ('GLD', 'fp'), 'największy fundusz grupy (300 × 400 > 60 × 700)')
        self.assertNotIn('fp_ustl', by, 'v94: bez obligacji — wypłata odsetek obniża NAV'); self.assertNotIn('fp_tech', by); self.assertNotIn('fp_us', by, 'akcje USA są już na liście cen krajów (SPY)')
        self.assertTrue(any(n.startswith('trendy XLK: skok NAV bez podziału') for n in zd.META['notes']))
        fu2 = {'XLE': {'h': [[d.isoformat(), (90.0 if i < 200 else 45.0) + 0.01 * i, 300 if i < 200 else 600] for i, d in enumerate(days)]}}
        with mock.patch.object(zd, '_now_utc', return_value=datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)):
            r = {x['id']: x for x in zd.build_trendy({'fundusze': {'f': fu2}})['p']}['fp_energy']
        self.assertTrue(abs(r['pr']) < 5 and abs(r['w']) < 5, 'podział 2:1 nie jest spadkiem ceny o połowę')
        self.assertEqual(zd.fund_split([0, 90.0, 300], [0, 45.0, 600]), 2); self.assertEqual(zd.fund_split([0, 45.0, 600], [0, 90.0, 300]), 0.5)
        self.assertEqual(by['fp_gold']['date'], days[-1].isoformat())
        self.assertEqual([b['id'] for b in out['b'] if b['id'] == 'px'], [], 'ceny funduszy nie wchodzą do „14 rynków akcji”')


class FunduszeV94(unittest.TestCase):
    """v94 (po drugim przeglądzie): zaokrąglenie liczby jednostek z zestawienia, luka w sesjach naprawiana pełnym plikiem, dni innego
    kalendarza w grupie, podziały 3:2, dni bez zmian w regule większości, krótka historia surowców, zły format daty CFTC."""

    def test_screener_rounding_and_rows(self):
        j = {'1': {'localExchangeTicker': 'EFA', 'portfolioId': '1', 'navAmount': {'r': 90.0}, 'navAmountAsOf': {'r': 20260924},
                   'totalNetAssetsFund': {'r': 90.0 * 739199996.8}, 'totalNetAssetsFundAsOf': {'r': 20260924}}}
        self.assertEqual(zd.parse_ishares_screener(j)['EFA'][3], 739200000, 'kilka jednostek różnicy z zaokrąglenia NAV — liczba z pełnymi tysiącami')
        h = [['2026-09-04', 10.0, 100], ['2026-09-07', 10.0, 100], ['2026-09-08', 11.0, 110]]
        self.assertEqual(zd._fund_rows(h), [h[0], h[2]], 'powtórzony wiersz (dzień wolny w USA w pliku złota) pominięty')

    def test_splits_and_clear(self):
        self.assertEqual(zd.fund_split([0, 30.0, 300], [0, 20.0, 450]), 1.5, 'podział 3:2')
        self.assertEqual(zd.fund_split([0, 20.0, 450], [0, 30.0, 300]), 1 / 1.5, 'scalenie 2:3')
        self.assertEqual(zd.fund_split([0, 30.0, 300], [0, 30.0, 3000]), 0, 'liczba ×10 przy tym samym NAV — błąd pliku, dzień pominięty')
        self.assertEqual(zd.fund_split([0, 30.0, 300], [0, 30.3, 360]), 1, 'duży, ale zwykły napływ (+20%)')
        self.assertTrue(zd._tr_clear(300, 400, [300, 0, 0, 0, 0]), 'jeden dzień tworzenia jednostek i dni bez zmian — wyraźny kierunek')
        self.assertFalse(zd._tr_clear(300, 400, [500, -50, -50, -50, -50]), 'dni ze zmianą w różne strony — bez wyraźnego kierunku')
        W = zd._iso_weeks(['2026-08-07', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14'], [1, 1, 1, 1, 1, 1], datetime.date(2026, 9, 1))
        self.assertEqual([w[0].isoformat() for w in W], ['2026-08-10'], 'niepełny pierwszy tydzień historii (1 dzień) pominięty')

    def test_group_other_calendar(self):
        mk = lambda rows: {'h': rows}
        fu = {'GLD': mk([['2026-09-03', 10.0, 100], ['2026-09-04', 10.0, 110], ['2026-09-07', 10.1, 110], ['2026-09-08', 10.0, 130]]),
              'IAU': mk([['2026-09-03', 5.0, 100], ['2026-09-04', 5.0, 90], ['2026-09-08', 5.0, 120]])}
        ds, v, _ = zd.fund_group(fu, ('GLD', 'IAU'))
        self.assertEqual(ds, ['2026-09-04', '2026-09-08'], 'dzień tylko w jednym pliku nie robi luki w grupie')
        self.assertAlmostEqual(v[1], ((130 - 110) * 10.0 + (120 - 90) * 5.0) / 1e6, msg='suma przez dzień wolny — dokładna')

    def test_gap_repaired_by_backfill(self):
        now = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)
        spy = [[d, 700.0 + i, 1000000 + i] for i, d in enumerate(['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'])]
        prev = {'scr_at': None, 'f': {t: {'iss': 'ssga', 'at': (now - datetime.timedelta(hours=1)).isoformat(), 'h': spy} for t in zd.FUND_SSGA}}
        hist = [['2026-09-%02d' % d, 50.0, 1000000] for d in (14, 15, 16, 17, 18, 21, 22)]
        prev['f']['EFA'] = {'iss': 'ishares', 'pid': '1', 'bf_done': True, 'h': hist}
        scr = {'1': {'localExchangeTicker': 'EFA', 'portfolioId': '1', 'navAmount': {'r': 50.0}, 'navAmountAsOf': {'r': 20260924},
                     'totalNetAssetsFund': {'r': 50.0 * 1200000}, 'totalNetAssetsFundAsOf': {'r': 20260924}}}
        doc = FunduszeV90.xml([(datetime.date(2026, 9, d).strftime('%b %d, %Y'), 50.0, 1000000 + 50000 * (d - 21)) for d in (24, 23, 22, 21, 18, 17, 16, 15, 14, 11, 10, 9, 8, 4, 3, 2, 1)
                               ] + [(datetime.date(2026, 8, d).strftime('%b %d, %Y'), 50.0, 1000000) for d in (31, 28, 27, 26, 25)])
        for fail in (False, True):
            calls = []
            def fake(url, timeout=60, headers=None):
                calls.append(url)
                if 'product-screener' in url:
                    return json.dumps(scr).encode()
                if fail:
                    raise RuntimeError('503')
                return doc
            zd.META['notes'].clear(); zd.META['errors'].clear()
            with mock.patch.object(zd, 'get_bytes', side_effect=fake), mock.patch.object(zd, '_now_utc', return_value=now), \
                    mock.patch.object(zd, 'FUND_SLEEP', 0), mock.patch.object(zd.time, 'sleep'):
                out = zd.build_fundusze(prev)
            e = out['f']['EFA']
            self.assertEqual(sum('get-fund-document' in u for u in calls), 1, 'luka 22.09 → 24.09 (brak 23.09) — pobranie pełnego pliku')
            if fail:
                self.assertEqual(e['h'][-1][0], '2026-09-22', 'bez pełnego pliku dzień z zestawienia nie jest dopisywany nad luką')
                self.assertTrue(e.get('bf_need') and e.get('bf_err_at'))
            else:
                self.assertEqual([r[0] for r in e['h'][-3:]], ['2026-09-22', '2026-09-23', '2026-09-24'])
                self.assertNotIn('bf_need', e)

    def test_surowce_short_history_and_bad_date(self):
        rows = SurowceV92.row('088691', '2026-09-15', 1000)
        rows[zd.CFTCD_COLS.index('Report_Date_as_YYYY-MM-DD')] = '09/15/2026'
        p, bad = zd.parse_cftcd(SurowceV92().text([rows]))
        self.assertEqual((p, bad), ({}, ['088691 09/15/2026 (data)']), 'nieznany format daty — wiersz pominięty, nie wyjątek')
        week = SurowceV92().text([SurowceV92.row('088691', '2026-09-15', 1000)], header=False).encode()
        prev = {'markets': {'gold': {'hist': {'dates': ['2026-06-%02d' % d for d in range(1, 14)]}}}}
        def fetch(u):
            if u == zd.CFTCD_WEEK_URL:
                return week
            raise RuntimeError('timeout')
        zd.META['errors'].clear()
        with self.assertRaises(RuntimeError):
            zd.build_surowce(fetch=fetch, today=datetime.date(2026, 9, 25), prev=prev)   # bez pliku rocznego: 1 raport zamiast 13 — zostaje poprzedni plik
        self.assertTrue(any('rok 2026' in e for e in zd.META['errors']))


def _wdays(a, b):
    d0, d1 = datetime.date.fromisoformat(a), datetime.date.fromisoformat(b)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1) if (d0 + datetime.timedelta(days=i)).weekday() < 5]


class HistoriaV95(unittest.TestCase):
    """v95–v95.2: historia wstecz — Indie z archiwum NSDL (miesiąc przyjęty tylko, gdy suma dni = suma miesiąca; nieudany miesiąc nie blokuje
    starszych), Tajwan i Hongkong do 26 tygodni (twarde limity czasu, bez stałej granicy po jednym braku pliku, święto TWSE wstecz po dwóch
    odpowiedziach), Brazylia ponad rok; „czy tydzień zapowiadał następny” także dla dziennych przepływów krajów."""
    FORM = ('<form><input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="a&amp;b" />'
            '<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="G" />'
            '<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="E" /></form>')
    T0 = datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)

    @staticmethod
    def arch(days, month_total, month='August'):
        """Strona archiwum NSDL: dni (data, akcje, razem), potem bloki „Total for <miesiąc>” i „Total for 2026” (jak na prawdziwej stronie)."""
        h = '<html><body><table>'
        for d, eq, tot in days:
            h += (f'<tr><td rowspan="3">{d}</td><td>Equity</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>{eq}</td><td>Rs.95.5614</td></tr>'
                  f'<tr><td>Sub-total</td><td>1</td><td>1</td><td>1</td><td>{eq}</td></tr>'
                  f'<tr><td>Total</td><td>1</td><td>1</td><td>1</td><td>{tot}</td></tr>')
        return h + (f'<tr><td rowspan="3">Total for {month}</td><td>Equity</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>999.00</td><td>&nbsp;</td></tr>'
                    '<tr><td>Sub-total</td><td>1</td><td>1</td><td>1</td><td>999.00</td></tr>'
                    '<tr><td>Debt-General Limit</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>500.00</td></tr>'
                    '<tr><td>Sub-total</td><td>1</td><td>1</td><td>1</td><td>500.00</td></tr>'
                    f'<tr><td>Total</td><td>1</td><td>1</td><td>1</td><td>{month_total}</td></tr>'
                    '<tr><td>Total for 2026</td><td>Equity</td><td>Stock Exchange</td><td>1</td><td>1</td><td>1</td><td>5000.00</td></tr>'
                    '<tr><td>Total</td><td>1</td><td>1</td><td>1</td><td>7,777.00</td></tr>'
                    '<tr><td>Reporting Date</td><td>Derivative Products</td></tr></table></body></html>')

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear(); zd._RUN_T0[0] = None; zd._BACK_LATE_NOTE[0] = False

    def at(self, now):
        """Wspólne atrapy czasu: data dnia stała (25.09, 17:00 w Tajpej), NOW przebiegu — podany."""
        return (mock.patch.object(zd.time, 'sleep', lambda s: None), mock.patch.object(zd, 'NOW', now),
                mock.patch.object(zd, '_now_utc', lambda: self.T0))

    def test_archive_month_and_year_totals_do_not_leak_into_last_day(self):
        tot = {}
        rows = zd.parse_nsdl_html(self.arch([('28-Aug-2026', '10.00', '12.00'), ('31-Aug-2026', '(134.81)', '(139.40)')], '(127.40)'), tot)
        self.assertEqual([r[0] for r in rows], ['2026-08-28', '2026-08-31'])
        self.assertEqual(rows[-1][1:5], [-134.81, None, None, -139.4], 'suma miesiąca nie jest dopisana do 31 sierpnia')
        self.assertEqual(tot, {'august': -127.4, '2026': 7777.0})

    def test_month_arithmetic(self):
        self.assertEqual((zd._ym_add('2026-01', -1), zd._ym_add('2026-09', -12), zd._ym_add('2025-12', 1)), ('2025-12', '2025-09', '2026-01'))

    def test_nsdl_http_sends_session_cookies_with_the_form(self):
        seen = []

        class R:
            def __init__(self, body, ck):
                self.body, self.headers = body, mock.Mock(get_all=lambda n: ck)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body

        def uo(req, timeout=30):
            seen.append((req.get_method(), dict(req.header_items()), req.data, timeout))
            return R(b'formularz', ['ASP.NET_SessionId=abc; path=/; HttpOnly', 'NL01ba3203=xyz; Path=/']) if req.data is None else R(b'wynik', None)
        with mock.patch.object(zd.urllib.request, 'urlopen', uo):
            b1, ck = zd.nsdl_http(zd.NSDL_ARCH, timeout=15)
            b2, ck2 = zd.nsdl_http(zd.NSDL_ARCH, {'hdnDate': '31-Aug-2026', 'x': 'a b'}, ck, timeout=30)
        self.assertEqual((b1, ck, b2, ck2), (b'formularz', 'ASP.NET_SessionId=abc; NL01ba3203=xyz', b'wynik', 'ASP.NET_SessionId=abc; NL01ba3203=xyz'))
        m, h, data, to = seen[1]
        self.assertEqual((m, h.get('Cookie'), h.get('Content-type'), data, to),
                         ('POST', 'ASP.NET_SessionId=abc; NL01ba3203=xyz', 'application/x-www-form-urlencoded', b'hdnDate=31-Aug-2026&x=a+b', 30))
        self.assertEqual((seen[0][0], seen[0][3]), ('GET', 15))

    def test_nsdl_month_posts_last_day_and_checks_month_total(self):
        sent = []

        def http(url, form=None, cookie='', timeout=30):
            if form is None:
                return self.FORM.encode(), 'S=1'
            sent.append((dict(form), cookie, timeout))
            return self.arch([('03-Aug-2026', '1.00', '2.00'), ('31-Aug-2026', '3.00', '4.00'), ('01-Sep-2026', '5.00', '6.00')], '6.00').encode(), cookie
        with mock.patch.object(zd, 'nsdl_http', http):
            rows = zd.nsdl_month('2026-08')
            self.assertRaises(RuntimeError, zd.nsdl_month, '2025-12')      # w odpowiedzi nie ma dni grudnia
        self.assertEqual([r[0] for r in rows], ['2026-08-03', '2026-08-31'], 'tylko dni tego miesiąca')
        f, ck, to = sent[0]
        self.assertEqual((f['hdnDate'], f['__EVENTTARGET'], f['__VIEWSTATE'], f['__EVENTVALIDATION'], ck, to), ('31-Aug-2026', 'btnSubmit1', 'a&b', 'E', 'S=1', 30))
        self.assertEqual(sent[1][0]['hdnDate'], '31-Dec-2025')
        bad = lambda url, form=None, cookie='', timeout=30: (self.FORM.encode(), '') if form is None else (self.arch([('03-Aug-2026', '1.00', '2.00')], '9.00').encode(), '')
        with mock.patch.object(zd, 'nsdl_http', bad):
            with self.assertRaisesRegex(RuntimeError, 'suma dni'):
                zd.nsdl_month('2026-08')          # suma dni 2 ≠ suma miesiąca 9 — miesiąc odrzucony
        with mock.patch.object(zd, 'nsdl_http', lambda url, form=None, cookie='', timeout=30: (b'<html>nowy formularz</html>', '')):
            with self.assertRaisesRegex(RuntimeError, 'formularza'):
                zd.nsdl_month('2026-08')

    def test_nsdl_part_failed_month_does_not_block_older_ones(self):
        asked = []

        def month(ym):
            asked.append(ym)
            if ym == '2026-07':
                raise RuntimeError('suma dni ≠ suma miesiąca')
            return [[ym + '-15', 1.0, 2.0, 0.0, 3.0, 95.0]]
        prev = {'arch': ['2026-08', '1999-01'], 'd': [['2026-06-15', 9.0, 9.0, 9.0, 9.0, 90.0], ['2026-09-01', 1.0, 1.0, 1.0, 1.0, 95.0]]}
        page = mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: ObceV54.NSDL.encode())
        with page, mock.patch.object(zd, 'nsdl_month', month), mock.patch.object(zd, 'NOW', '2026-09-25T09:00:00+00:00'):
            out = zd.nsdl_part(prev)
            self.assertEqual(asked, ['2026-07', '2026-06', '2026-05'], 'nieudany miesiąc nie zatrzymuje starszych; najwyżej 3 próby')
            self.assertEqual(out['arch'], ['2026-05', '2026-06', '2026-08'], 'spoza 12 miesięcy — usunięty')
            self.assertEqual(out['afail'], {'2026-07': '2026-09-25T09:00:00+00:00'})
            m = {r[0]: r for r in out['d']}
            self.assertEqual(m['2026-06-15'][1], 1.0, 'archiwum zastępuje dzień zebrany wcześniej')
            self.assertTrue({'2026-09-01', '2026-09-23', '2026-09-24'} <= set(m))
            self.assertTrue(any(n.startswith('NSDL archiwum 2026-07') and 'jutro' in n for n in zd.META['notes']))
            self.assertFalse(zd.META['errors'], 'brak starszego miesiąca to notatka, nie błąd bieżących danych')
            asked.clear(); out2 = zd.nsdl_part(out)
            self.assertEqual(asked, ['2026-04', '2026-03', '2026-02'], 'miesiąc odrzucony dziś czeka do jutra')

            def down(ym):
                asked.append(ym); raise zd.urllib.error.URLError('timed out')
            asked.clear()
            with mock.patch.object(zd, 'nsdl_month', down):
                out3 = zd.nsdl_part(out2)
            self.assertEqual(asked, ['2026-01'], 'awaria sieci — przerwa do następnego przebiegu')
            self.assertNotIn('2026-01', out3.get('afail', {}))
        with page, mock.patch.object(zd, 'nsdl_month', month), mock.patch.object(zd, 'NOW', '2026-09-26T09:00:00+00:00'):
            asked.clear(); zd.nsdl_part(out)
        self.assertEqual(asked[0], '2026-07', 'następnego dnia — ponowna próba')

    def test_tw_back_newest_first_skips_known(self):
        now = datetime.datetime(2026, 9, 25, 17, 0)
        self.assertEqual(zd.tw_back({'2026-09-24'}, {'2026-09-23'}, now, skip={'2026-09-22', '2026-09-17'})[:4],
                         ['2026-09-21', '2026-09-18', '2026-09-16', '2026-09-15'])
        b = zd.tw_back(set(), set(), now)
        self.assertEqual(b, sorted(b, reverse=True)); self.assertEqual(b[-1], '2026-03-27', '182 dni wstecz')

    def test_twse_backfill_holiday_needs_two_answers_12h_apart(self):
        prev = {'d': [[d, 1.0, 0, 0, 0] for d in _wdays('2026-09-11', '2026-09-23')], 'empty': []}
        asked, tos = [], []

        def gj(url, headers=None, timeout=30):
            if 'DEXTAUS' in url:
                self.assertIn('limit=200', url)
                return {'observations': [{'date': '2026-07-01', 'value': '31.0'}]}
            day = url.split('dayDate=')[1][:8]; iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'; asked.append(iso); tos.append(timeout)
            if iso == '2026-09-24':
                return {'stat': 'Busy'}                          # ostatni dzień: dziwna odpowiedź = błąd, nie święto
            if iso == '2026-09-10':
                return {'stat': 'OK', 'date': '20260910', 'data': []}   # starszy dzień bez danych — nie święto
            if iso == '2026-09-09':
                return {'stat': 'No Data!'}
            return ObceV54.tw(iso)
        a, b, c = self.at('2026-09-25T09:00:00+00:00')
        with a, b, c, mock.patch.object(zd, 'get_json', gj):
            out = zd.twse_part(prev, 'KLUCZ')
        self.assertEqual(len(asked), 2 + zd.TW_BACK_MAX); self.assertEqual(asked[:2], ['2026-09-24', '2026-09-25'], 'najpierw ostatnie dni')
        self.assertEqual((asked[2], asked[-1]), ('2026-09-10', '2026-08-26'), 'potem wstecz, od najnowszego, najwyżej 12 dni')
        self.assertEqual((tos[:2], set(tos[2:])), ([30, 30], {zd.TW_BACK_TIMEOUT}), 'zapytania wstecz z krótkim limitem czasu')
        days = {r[0] for r in out['d']}
        self.assertNotIn('2026-09-09', out['empty'], 'jedna odpowiedź „No Data!” za starszy dzień to jeszcze nie święto')
        self.assertEqual(out['pend'], {'2026-09-09': '2026-09-25T09:00:00+00:00'})
        self.assertTrue({'2026-08-26', '2026-09-08', '2026-09-25'} <= days); self.assertFalse({'2026-09-10', '2026-09-24', '2026-09-09'} & days)
        self.assertTrue(any(e.startswith('TWSE: 1 dni') and 'Busy' in e for e in zd.META['errors']))
        self.assertTrue(any(n.startswith('TWSE historia wstecz: 1 dni') for n in zd.META['notes']))
        self.assertEqual({r[0]: r[5] for r in out['d']}['2026-08-26'], round(-32964.6 / 31.0, 1), 'starszy dzień też przeliczony na USD')
        asked.clear()
        a, b, c = self.at('2026-09-25T10:00:00+00:00')
        with a, b, c, mock.patch.object(zd, 'get_json', gj):
            out2 = zd.twse_part(out, 'KLUCZ')
        self.assertNotIn('2026-09-09', asked, 'po godzinie — jeszcze nie pytamy ponownie')
        asked.clear()
        a, b, c = self.at('2026-09-25T22:00:00+00:00')
        with a, b, c, mock.patch.object(zd, 'get_json', lambda url, headers=None, timeout=30: {'stat': 'No Data!'} if '20260924' in url else gj(url, headers, timeout)):
            out3 = zd.twse_part(out2, 'KLUCZ')
        self.assertIn('2026-09-09', out3['empty'], 'druga odpowiedź „No Data!” po 13 h — święto')
        self.assertNotIn('pend', out3)
        self.assertIn('2026-09-24', out3['empty'], 'ostatni dzień: „No Data!” od razu = dzień bez sesji (jak dotąd)')

    def test_hkex_backfill_404_is_retried_later_and_three_in_a_row_stop(self):
        prev = {'d': [[d, 1.0, 2.0, 1.0, 2] for d in _wdays('2026-09-11', '2026-09-24')], 'empty': []}
        asked, gone = [], {'2026-09-08'}

        def gb(url, headers=None, timeout=60):
            d = url.split('daily_')[1][:8]; iso = f'{d[:4]}-{d[4:6]}-{d[6:]}'; asked.append(iso)
            if iso == '2026-09-25' or iso in gone or iso < '2026-09-01':
                raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
            return HkexV67.JS.replace('2026-09-24', iso).encode()

        def run(prev, now):
            a, b, c = self.at(now)
            with a, b, c, mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, 'get_json', lambda url, headers=None, timeout=30: {'observations': []}):
                return zd.hkex_part(prev, 'KLUCZ')
        out = run(prev, '2026-09-25T09:00:00+00:00')
        days = {r[0] for r in out['d']}
        self.assertTrue({'2026-09-07', '2026-09-01'} <= days, 'jeden brak pliku nie zatrzymuje starszych dni')
        self.assertEqual(set(out['nf']), {'2026-09-08', '2026-08-31', '2026-08-28', '2026-08-27'})
        self.assertNotIn('2026-08-26', asked, '3 braki pliku z rzędu — koniec na ten przebieg')
        self.assertFalse(zd.META['errors'], 'brak starszego pliku to nie błąd')
        self.assertTrue(any(n.startswith('HKEX historia wstecz: brak pliku za 4 dni') for n in zd.META['notes']))
        asked.clear(); out2 = run(out, '2026-09-26T09:00:00+00:00')
        self.assertEqual(asked, ['2026-09-25', '2026-08-26', '2026-08-25', '2026-08-24'], 'dni bez pliku odłożone na tydzień; dalej wstecz')
        gone.clear(); asked.clear(); out3 = run(out2, '2026-10-03T10:00:00+00:00')
        self.assertIn('2026-09-08', asked, 'po tygodniu — ponowna próba')
        self.assertIn('2026-09-08', {r[0] for r in out3['d']})

    def test_backfill_hard_time_budget_and_long_run(self):
        """v95.2–v95.4: budżet liczony razem z najdłuższym możliwym zapytaniem; przebieg dłuższy niż 9 min — bez historii wstecz (z notatką)."""
        prev = {'d': [[d, 1.0, 0, 0, 0] for d in _wdays('2026-09-11', '2026-09-23')], 'empty': []}
        asked = []

        def gj(url, headers=None, timeout=30):
            if 'DEXTAUS' in url:
                return {'observations': []}
            day = url.split('dayDate=')[1][:8]; asked.append(f'{day[:4]}-{day[4:6]}-{day[6:]}')
            return ObceV54.tw(asked[-1])
        clock = iter(range(0, 10000, 15))                  # każde sprawdzenie czasu = +15 s
        a, b, c = self.at('2026-09-25T09:00:00+00:00')
        with a, b, c, mock.patch.object(zd, 'get_json', gj), mock.patch.object(zd.time, 'monotonic', lambda: next(clock)):
            zd.twse_part(prev, '')
        self.assertEqual(asked, ['2026-09-24', '2026-09-25', '2026-09-10'], '15 s + 12 s ≤ 40 s — tak; 30 s + 12 s > 40 s — stop')
        asked.clear(); zd._RUN_T0[0] = zd.time.monotonic() - 700
        a, b, c = self.at('2026-09-25T09:00:00+00:00')
        with a, b, c, mock.patch.object(zd, 'get_json', gj):
            zd.twse_part(prev, '')
        self.assertEqual(asked, ['2026-09-24', '2026-09-25'], 'długi przebieg — tylko ostatnie dni')
        self.assertEqual(sum('historia wstecz pominięta' in n for n in zd.META['notes']), 1, 'jedna notatka z czasem przebiegu')
        zd._RUN_T0[0] = None; zd._BACK_LATE_NOTE[0] = False
        months = []

        def month(ym):
            months.append(ym); return [[ym + '-15', 1.0, 2.0, 0.0, 3.0, 95.0]]
        clock = iter([0, 0, 40, 80, 120])
        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: ObceV54.NSDL.encode()), mock.patch.object(zd, 'nsdl_month', month), \
                mock.patch.object(zd.time, 'monotonic', lambda: next(clock)):
            out = zd.nsdl_part({'d': []})
        self.assertEqual((months, out['arch']), (['2026-08'], ['2026-08']), 'drugi miesiąc: 40 s + 45 s > 60 s — w następnym przebiegu')

    def test_brazil_asks_for_more_than_a_year(self):
        self.assertGreaterEqual(zd.BCB_DAYS, 365)

    def test_base_rate_for_daily_country_flows(self):
        days = _wdays('2026-03-02', '2026-09-18')
        sign = lambda d: 1 if (datetime.date.fromisoformat(d).isocalendar()[1] // 3) % 2 else -1   # kierunek zmienia się co 3 tygodnie
        ob = {'in': {'d': [[d, 50.0 * sign(d), 10.0, 0, 60.0, 95.0] for d in days]},
              'br': {'d': [[d, -30.0 * sign(d), 0, 0, 0, 0] for d in days]},
              'tw': {'d': [[d, 900.0 * sign(d), 0, 0, 0] for d in days if d != '2026-06-10'], 'empty': []},
              'hk': {'d': [[d, 70.0 * sign(d), 0, 0, 2] for d in days if d != '2026-07-01'], 'empty': ['2026-07-01']}}
        with mock.patch.object(zd, '_now_utc', lambda: self.T0):
            out = zd.build_trendy({'obce': ob})
            today = datetime.date(2026, 9, 25)
            exp = [zd.trend_persist(*zd._tr_cols(ob['in'], 1), today), zd.trend_persist(*zd._tr_cols(ob['in'], 2), today),
                   zd.trend_persist(*zd._tr_sessions(ob['tw'], 1), today), zd.trend_persist(*zd._tr_sessions(ob['hk'], 1), today),
                   zd.trend_persist(*zd._tr_cols(ob['br'], 1), today)]
        b = {x['id']: x for x in out['b']}['ob']
        self.assertEqual((b['k'], b['n']), (sum(e[0] for e in exp), sum(e[1] for e in exp)))
        self.assertTrue(all(e[1] for e in exp), 'każda z 5 serii wnosi przypadki')
        self.assertGreater(b['n'], b['weeks'], 'kilka serii w tym samym tygodniu — niepewność liczona na tygodnie')
        self.assertEqual(b['ci'], list(zd.wilson(b['k'], b['n'], n_eff=b['weeks'])))
        self.assertLess(exp[2][1], exp[4][1], 'Tajwan: tydzień z brakującą sesją (10.06) nie jest liczony')
        self.assertEqual(exp[3][1], exp[4][1], 'Hongkong: znany dzień bez sesji (1.07) nie psuje tygodnia')
        self.assertNotIn('ob', {x['id'] for x in zd.build_trendy({'obce': {}})['b']})

import re as re   # v96: test ikon (moduł testów nie importował re)


class IkonyV96(unittest.TestCase):
    """v96: każda ikona, o którą prosi strona (flagi, krypto, sieci, giełdy, glify), istnieje w img/ i jest bezpiecznym SVG
    (bez skryptów, zdarzeń i odwołań na zewnątrz); publikacja kopiuje img/ i przerywa się bez ikon."""
    ROOT = os.path.dirname(os.path.abspath(__file__))

    def setUp(self):
        self.html = open(os.path.join(self.ROOT, 'index.html'), encoding='utf-8').read()

    def test_every_referenced_icon_file_exists(self):
        h = self.html
        flags = re.search(r"const FLAGS_OK=new Set\('([a-z ]+)'\.split", h).group(1).split()
        self.assertGreater(len(flags), 240)
        for c in flags + ['eu']:
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'flagi', c + '.svg')), c)
        for grp in re.findall(r"REGF=\{(.*?)\};", h)[:1]:
            for c in re.findall(r"'([a-z]{2})'", grp):
                self.assertIn(c, flags, 'flaga regionu ' + c)
        ccy = re.search(r"const CCY=\{(.*?)\};", h).group(1)
        for c in re.findall(r"\[\s*'([a-z]{2})'\s*,", ccy):
            self.assertIn(c, flags, 'flaga waluty ' + c)
        for c in re.search(r"const CRYPTO_SVG=new Set\('([a-z ]+)'\.split", h).group(1).split():
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'krypto', c + '.svg')), c)
        for c in set(re.findall(r":'([a-z0-9-]+)'", re.search(r"const NET_SVG=\{(.*?)\};", h).group(1))):
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'sieci', c + '.svg')), c)
        for c in set(re.findall(r":'([a-z0-9-]+)'", re.search(r"const EXCH_SVG=\{(.*?)\};", h).group(1))):
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'gieldy', c + '.svg')), c)
        self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'sieci', 'hyper-evm.svg')))
        for g in set(re.findall(r"glyphImg\('([a-z]+)'", h)):
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'glify', g + '.svg')), 'glif ' + g)
        self.assertTrue(os.path.isfile(os.path.join(self.ROOT, 'img', 'LICENCJE.txt')))

    def test_icon_files_are_safe_svg(self):
        bad = re.compile(r'<script|\bon[a-z]+\s*=|javascript:|<foreignObject|<image\b|<!ENTITY|@import|href\s*=\s*["\'](?!#)|url\((?!#)', re.I)
        n = 0
        for d in ('flagi', 'krypto', 'sieci', 'gieldy', 'glify'):
            for f in os.listdir(os.path.join(self.ROOT, 'img', d)):
                t = open(os.path.join(self.ROOT, 'img', d, f), encoding='utf-8').read()
                self.assertTrue(f.endswith('.svg') and t.lstrip().startswith('<svg'), f)
                self.assertIsNone(bad.search(t), d + '/' + f)
                n += 1
        self.assertGreater(n, 300)

    def test_publication_copies_icons(self):
        w = open(os.path.join(self.ROOT, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        self.assertIn('cp -r img _site/img', w)
        self.assertIn('test -f _site/img/flagi/pl.svg', w)


class UsaV97(unittest.TestCase):
    """v97: EIA (energia), BLS (makro USA), BEA (bilans płatniczy USA) — przykładowe odpowiedzi, bez sieci; brak nie jest zerem;
    klucze nigdy w plikach wynikowych ani w komunikatach."""
    KEY = 'SEKRET-KLUCZ-123'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear()
        self._sec = list(zd.SECRETS); zd.SECRETS[:] = [self.KEY]

    def tearDown(self):
        zd.SECRETS[:] = self._sec

    @staticmethod
    def eia(sid, rows):
        return {'response': {'total': '9983', 'frequency': 'daily', 'data': [
            {'period': d, 'series': sid, 'value': v, 'units': '$/BBL'} for d, v in rows]},
            'warnings': [{'warning': 'incomplete return', 'description': 'The API can only return 5000 rows'}]}

    def test_eia_series_url_parse_and_missing_values(self):
        seen = []

        def gj(url, headers=None, timeout=30):
            seen.append(url)
            return self.eia('RWTC', [('2026-09-22', '96.41'), ('2026-09-21', '96.97'), ('2026-09-19', None), ('2026-09-18', '-')])
        with mock.patch.object(zd, 'get_json', gj):
            d, unit = zd.eia_series(self.KEY, 'petroleum/pri/spt', 'daily', 'RWTC', 90)
        self.assertEqual(d, [['2026-09-21', 96.97], ['2026-09-22', 96.41]], 'rosnąco; brak i „-” pominięte, nie zero')
        self.assertEqual(unit, '$/BBL')
        self.assertIn('https://api.eia.gov/v2/petroleum/pri/spt/data/?api_key=SEKRET-KLUCZ-123&frequency=daily&data%5B0%5D=value&facets%5Bseries%5D%5B%5D=RWTC', seen[0])
        self.assertIn('sort%5B0%5D%5Bdirection%5D=desc', seen[0])
        with mock.patch.object(zd, 'get_json', lambda url, headers=None, timeout=30: {'error': {'code': 'API_KEY_INVALID'}}):
            self.assertRaises(RuntimeError, zd.eia_series, self.KEY, 'petroleum/pri/spt', 'daily', 'RWTC', 90)
        with mock.patch.object(zd, 'get_json', lambda url, headers=None, timeout=30: self.eia('RBRTE', [('2026-09-22', '114.89')])):
            self.assertRaises(RuntimeError, zd.eia_series, self.KEY, 'petroleum/pri/spt', 'daily', 'RWTC', 90)   # inna seria = brak

    def test_build_energia_keeps_old_series_and_masks_key(self):
        def gj(url, headers=None, timeout=30):
            if 'RBRTE' in url:
                raise RuntimeError(f'HTTP Error 503 dla {url}')
            sid = re.search(r'facets%5Bseries%5D%5B%5D=([A-Z0-9]+)', url).group(1)
            return self.eia(sid, [('2026-09-18', '100'), ('2026-09-22', '101.5')])
        prev = {'s': {'brent': {'id': 'RBRTE', 'freq': 'daily', 'unit': '$/BBL', 'd': [['2026-09-19', 110.0]]}}}
        with mock.patch.object(zd, 'get_json', gj):
            out = zd.build_energia(self.KEY, prev)
        self.assertEqual(set(out['s']), {'wti', 'brent', 'gas', 'crude', 'spr'})
        self.assertEqual(out['s']['brent']['d'], [['2026-09-19', 110.0]], 'seria bez odpowiedzi — poprzednie wartości z datą')
        self.assertEqual(out['s']['wti']['d'][-1], ['2026-09-22', 101.5])
        self.assertTrue(any(e.startswith('EIA: 1 serie') for e in zd.META['errors']))
        self.assertFalse(any(self.KEY in e for e in zd.META['errors']), 'klucz nigdy w komunikatach')
        self.assertNotIn(self.KEY, json.dumps(out))
        with mock.patch.object(zd, 'get_json', lambda url, headers=None, timeout=30: {'error': 'x'}):
            self.assertRaises(RuntimeError, zd.build_energia, self.KEY, None)

    def test_bls_payload_missing_month_yoy_and_payroll_change(self):
        sent = []

        def pj(url, obj, timeout=60):
            sent.append(obj)
            mk = lambda sid, vals: {'seriesID': sid, 'data': [{'year': y, 'period': p, 'value': v} for y, p, v in vals]}
            return {'status': 'REQUEST_SUCCEEDED', 'message': [], 'Results': {'series': [
                mk('CUUR0000SA0', [('2026', 'M08', '334.980'), ('2025', 'M08', '323.976'), ('2025', 'M10', '-'), ('2026', 'M10', '336'), ('2025', 'M13', '322')]),
                mk('LNS14000000', [('2026', 'M08', '4.4'), ('2026', 'M07', '4.3')]),
                mk('CES0000000001', [('2026', 'M06', '159500'), ('2026', 'M07', '159600'), ('2026', 'M08', '159650'), ('2026', 'M10', '159700')]),
                mk('CES0500000003', [('2026', 'M08', '37.10')])]}}
        with mock.patch.object(zd, 'post_json', pj):
            out = zd.build_usa_makro(self.KEY, None, today=datetime.date(2026, 9, 25))
        self.assertEqual(sent[0]['seriesid'][:2], ['CUUR0000SA0', 'CUUR0000SA0L1E'])
        self.assertEqual((sent[0]['startyear'], sent[0]['endyear'], sent[0]['registrationkey']), ('2024', '2026', self.KEY))
        cpi = out['s']['cpi']
        self.assertIn(['2025-10', None], cpi['d'], '„-” = brak, nie zero'); self.assertNotIn('2025-13', [d for d, _ in cpi['d']], 'M13 pominięte')
        self.assertEqual(cpi['yoy'], [['2026-08', round((334.98 / 323.976 - 1) * 100, 1)]], 'r/r tylko gdy są obie wartości (X 2026 bez X 2025)')
        self.assertEqual(out['s']['nfp']['chg'], [['2026-07', 100.0], ['2026-08', 50.0]], 'zmiana m/m tylko dla kolejnych miesięcy (IX brak)')
        self.assertNotIn('core', out['s']); self.assertTrue(any(e.startswith('BLS: brak serii CUUR0000SA0L1E') for e in zd.META['errors']))
        self.assertNotIn(self.KEY, json.dumps(out), 'klucz nie trafia do pliku')
        with mock.patch.object(zd, 'post_json', lambda url, obj, timeout=60: {'status': 'REQUEST_NOT_PROCESSED', 'message': ['daily threshold']}):
            self.assertRaisesRegex(RuntimeError, 'REQUEST_NOT_PROCESSED', zd.build_usa_makro, self.KEY, None)
        sent.clear()
        with mock.patch.object(zd, 'post_json', pj):
            zd.build_usa_makro('', None, today=datetime.date(2026, 9, 25))
        self.assertNotIn('registrationkey', sent[0], 'bez klucza — zapytanie publiczne (mniejszy limit)')

    def test_bea_units_errors_areas_and_gdp(self):
        def gj(url, headers=None, timeout=30):
            q = dict(zd.urllib.parse.parse_qsl(url.split('?', 1)[1]))
            self.assertEqual((q['UserID'], q['ResultFormat']), (self.KEY, 'JSON'))
            if q['method'] == 'GetParameterValues':
                vals = [{'Key': 'BalCurrAcct', 'Desc': 'Balance on current account'}, {'Key': 'FinAssetsExclFinDeriv', 'Desc': 'U.S. assets'},
                        {'Key': 'FinLiabsExclFinDeriv', 'Desc': 'U.S. liabilities'}] if q['ParameterName'] == 'Indicator' else \
                       [{'Key': 'AllCountries', 'Desc': 'All Countries Total'}, {'Key': 'China', 'Desc': 'China'}]
                return {'BEAAPI': {'Results': {'ParamValue': vals}}}
            if q.get('datasetname') == 'NIPA':
                self.assertEqual((q['TableName'], q['Frequency'], q['Year']), ('T10101', 'Q', '2023,2024,2025,2026'))
                return {'BEAAPI': {'Results': {'Data': [{'LineNumber': '1', 'TimePeriod': '2026Q2', 'DataValue': '3.8'},
                                                         {'LineNumber': '2', 'TimePeriod': '2026Q2', 'DataValue': '9.9'}]}}}
            if q['Indicator'] == 'NetLendBorrFinAcct':
                return {'BEAAPI': {'Results': {'Error': {'APIErrorCode': '1', 'APIErrorDescription': 'Invalid Indicator'}}}}
            if q['AreaOrCountry'] == 'All':
                self.assertEqual(q['Frequency'], 'QNSA')
                return {'BEAAPI': {'Results': {'Data': [{'AreaOrCountry': 'China', 'TimePeriod': '2026Q1', 'UNIT_MULT': '6', 'DataValue': '-1,234'},
                                                         {'AreaOrCountry': 'Europe', 'TimePeriod': '2026Q1', 'UNIT_MULT': '6', 'DataValue': '(D)'}]}}}
            return {'BEAAPI': {'Results': {'Data': [{'TimePeriod': '2026Q1', 'UNIT_MULT': '6', 'DataValue': '311,234'},
                                                     {'TimePeriod': '2026Q2', 'UNIT_MULT': '9', 'DataValue': '0.5'}]}}}
        with mock.patch.object(zd, 'get_json', gj):
            out = zd.build_bilans_usa(self.KEY, None, today=datetime.date(2026, 9, 25))
        self.assertEqual(out['ita']['BalCurrAcct'], [['2026-Q1', 311234.0], ['2026-Q2', 500.0]], 'mln USD: przecinki i UNIT_MULT')
        self.assertNotIn('NetLendBorrFinAcct', out['ita'])
        self.assertEqual(out['areas']['FinLiabsExclFinDeriv'], {'China': [['2026-Q1', -1234.0]]}, '„(D)” = brak, nie zero')
        self.assertEqual(out['gdp'], [['2026-Q2', 3.8]]); self.assertEqual(out['names']['China'], 'China')
        self.assertTrue(any(n.startswith('BEA: brak wskaźników NetLendBorrFinAcct') for n in zd.META['notes']))
        self.assertTrue(any(e.startswith('BEA: 1 zapytań bez danych') and 'Invalid Indicator' in e for e in zd.META['errors']))
        self.assertNotIn(self.KEY, json.dumps(out))
        bad = lambda url, headers=None, timeout=30: {'BEAAPI': {'Results': {'Error': {'APIErrorDescription': 'Invalid Request - Invalid API UserId.'}}}}
        with mock.patch.object(zd, 'get_json', bad):
            self.assertRaisesRegex(RuntimeError, 'Invalid API UserId', zd.build_bilans_usa, self.KEY, None)

    def test_main_block_cache_missing_key_and_failure_keeps_previous(self):
        saved = {}
        prev = {'energia': {'at': '2026-09-25T00:00:00+00:00', 's': {}}, 'bilans-usa': {'at': zd.NOW, 'ita': {}}}
        calls = []
        env = {'EIA_KEY': 'k1', 'BLS_KEY': '', 'BEA_KEY': ''}
        stubs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce',
                  'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk',
                  'build_fundusze', 'build_surowce', 'build_trendy', 'build_fred', 'build_etf', 'build_day', 'build_prices', 'build_cmc', 'build_oecd', 'build_rynki', 'build_indeksy', 'build_stres', 'build_wieloryby', 'build_dzwignia', 'build_ceny_krypto', 'build_insider')]
        for s in stubs:
            s.start()
        try:
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                    mock.patch.object(zd, 'previous', lambda n: prev.get(n)), \
                    mock.patch.object(zd, 'build_energia', lambda k, p: calls.append(('eia', k)) or (_ for _ in ()).throw(RuntimeError('HTTP 500'))), \
                    mock.patch.object(zd, 'build_usa_makro', lambda k, p: calls.append(('bls', k)) or {'at': 'x', 's': {}}), \
                    mock.patch.object(zd, 'build_bilans_usa', lambda k, p: calls.append(('bea', k)) or {}):
                zd.main()
        finally:
            for s in stubs:
                s.stop()
        self.assertEqual(calls, [('eia', 'k1'), ('bls', '')], 'BEA: plik młodszy niż doba — bez zapytań; BLS działa bez klucza')
        self.assertIs(saved['energia'], prev['energia'], 'awaria EIA — zostaje poprzedni plik')
        self.assertEqual((zd.META['ok']['eia'], zd.META['ok']['bls'], zd.META['ok']['bea']), (False, True, 'cached'))
        self.assertIn('EIA: HTTP 500', zd.META['errors'])


if __name__ == '__main__':
    unittest.main()


class OecdV99(unittest.TestCase):
    """v99: OECD na serwerze — kształt jak gOecd() na stronie, brak ≠ zero, część z błędem = poprzednia wersja z własnym czasem."""
    SD = {'data': {'structure': {'dimensions': {'observation': [
        {'id': 'REF_AREA', 'values': [{'id': 'USA'}, {'id': 'JPN'}]}, {'id': 'FREQ', 'values': [{'id': 'M'}]},
        {'id': 'TIME_PERIOD', 'values': [{'id': '2026-08'}, {'id': '2026-07'}]}]}},
        'dataSets': [{'observations': {'0:0:0': [230.7], '0:0:1': [224.2], '1:0:0': [None], '1:0:1': [True], }}]}}

    def test_start_like_page(self):
        self.assertEqual(zd.oecd_start(datetime.date(2026, 9, 25)), '2025-06')
        self.assertEqual(zd.oecd_start(datetime.date(2026, 1, 5)), '2024-10')
        self.assertEqual(zd.oecd_start(datetime.date(2026, 3, 31)), '2024-12')

    def test_parse_sorted_skip_missing(self):
        self.assertEqual(zd.oecd_parse(self.SD), {'USA': [['2026-07', 224.2], ['2026-08', 230.7]]})   # JPN: null i bool — brak, nie zero

    def test_iso_same_as_page(self):
        import re
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'), encoding='utf-8').read()
        g = html[html.index('const GREG='):]
        g = g[:g.index('];')]
        iso = []
        for m in re.finditer(r"iso:\[([^\]]*)\]", g):
            for x in re.findall(r"'([A-Z]{3})'", m.group(1)):
                if x not in iso:
                    iso.append(x)
        self.assertEqual(zd.OECD_ISO, iso)
        self.assertIn("srvJSON('oecd')", html)

    def test_build_ok_and_partial_failure(self):
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'oecd_get', return_value=self.SD), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_oecd(None, today=datetime.date(2026, 9, 25))
        self.assertEqual(o['ok'], {'share': True, 'irlt': True, 'cli': True})
        self.assertEqual(o['start'], '2025-06'); self.assertEqual(o['part_at']['cli'], zd.NOW)
        self.assertEqual(zd.META['errors'], [])
        prev = {'at': '2026-09-24T00:00:00+00:00', 'cli': {'USA': [['2026-06', 100.5]]}, 'part_at': {'cli': '2026-09-20T00:00:00+00:00'}}

        def fake(path, start, _retry=True):
            if 'DF_CLI' in path:
                raise zd.urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None)
            return self.SD
        with mock.patch.object(zd, 'oecd_get', side_effect=fake), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_oecd(prev, today=datetime.date(2026, 9, 25))
        self.assertEqual(o['ok']['cli'], False)
        self.assertEqual(o['cli'], prev['cli'], 'część z błędem — poprzednia wersja')
        self.assertEqual(o['part_at']['cli'], '2026-09-20T00:00:00+00:00', 'z własnym (starszym) czasem')
        self.assertTrue(any(e.startswith('OECD: cli:') for e in zd.META['errors']))
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'oecd_get', side_effect=RuntimeError('offline')), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_oecd(None, today=datetime.date(2026, 9, 25))   # bez indeksów giełdowych — nie zapisujemy pustego pliku
        zd.META['errors'].clear()

    def test_retry_once_on_429(self):
        calls = []

        def g(url, headers=None, timeout=30):
            calls.append(url)
            if len(calls) == 1:
                raise zd.urllib.error.HTTPError(url, 429, 'Too Many Requests', {}, None)
            return self.SD
        with mock.patch.object(zd, 'get_json', side_effect=g), mock.patch.object(zd.time, 'sleep'):
            self.assertEqual(zd.oecd_get(zd.OECD_Q['share'], '2025-06'), self.SD)
        self.assertEqual(len(calls), 2)
        self.assertIn('USA+CAN+BRA', calls[0]); self.assertIn('startPeriod=2025-06&format=jsondata', calls[0])

class RynkiV101(unittest.TestCase):
    """v101: kursy EBC i rentowności 10L na serwerze — kształt jak na stronie, brak ≠ zero, część z błędem = poprzednia wersja."""
    XML = ('<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
           'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">'
           '<entry><content type="application/xml"><m:properties><d:NEW_DATE m:type="Edm.DateTime">2026-09-25T00:00:00</d:NEW_DATE><d:BC_10YEAR m:type="Edm.Double">5.17</d:BC_10YEAR></m:properties></content></entry>'
           '<entry><content type="application/xml"><m:properties><d:NEW_DATE m:type="Edm.DateTime">2026-09-24T00:00:00</d:NEW_DATE><d:BC_10YEAR m:type="Edm.Double">5.12</d:BC_10YEAR></m:properties></content></entry>'
           '<entry><content type="application/xml"><m:properties><d:NEW_DATE m:type="Edm.DateTime">2026-09-23T00:00:00</d:NEW_DATE><d:BC_10YEAR m:null="true"/></m:properties></content></entry></feed>')
    BUBA = {'data': {'dataSets': [{'series': {'0:0': {'observations': {'0': ['3.55'], '1': ['3.60'], '2': [None]}}}}],
                     'structure': {'dimensions': {'observation': [{'values': [{'id': '2026-09-24'}, {'id': '2026-09-25'}, {'id': '2026-09-26'}]}]}}}}
    FX = {'amount': 1.0, 'base': 'USD', 'date': '2026-09-25', 'rates': {'EUR': 0.877}}

    def test_dates_like_page(self):
        self.assertEqual(zd.months_back(datetime.date(2026, 3, 31), 1), datetime.date(2026, 2, 28))
        self.assertEqual(zd.months_back(datetime.date(2026, 9, 25), 12), datetime.date(2025, 9, 25))
        self.assertEqual(zd.fx_dates(datetime.date(2026, 9, 25)), {'now': 'latest', '1M': '2026-08-25', '1Q': '2026-06-25', '1R': '2025-09-25', '1D': '2026-09-24', '1T': '2026-09-18'})

    def test_parsers(self):
        self.assertEqual(zd.ust_parse(self.XML), [['2026-09-24', 5.12], ['2026-09-25', 5.17]])   # brak wartości — brak wiersza, nie zero
        self.assertEqual(zd.buba_parse(self.BUBA), [['2026-09-24', 3.55], ['2026-09-25', 3.6]])

    def test_build_parts_and_fallback(self):
        zd.META['errors'].clear()
        calls = []

        def gj(url, headers=None, timeout=30):
            calls.append(url)
            return self.BUBA if 'bundesbank' in url else self.FX

        def gt(url, headers=None, timeout=30):
            calls.append(url)
            return 200, self.XML
        with mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd, 'get', side_effect=gt):
            o = zd.build_rynki(None, today=datetime.date(2026, 9, 26))
        self.assertEqual(o['ok'], {'fx': True, 'ust': True, 'buba': True})
        self.assertEqual(sorted(o['fx']), ['1D', '1M', '1Q', '1R', '1T', 'now'])
        self.assertEqual(sum('field_tdr_date_value=2025' in c for c in calls), 1, 'poprzedni rok pobrany, gdy go brak')
        self.assertIn('startPeriod=2025-08-26', [c for c in calls if 'bundesbank' in c][0])
        prev = {'at': '2026-09-25T00:00:00+00:00', 'fx': {'now': self.FX}, 'part_at': {'fx': '2026-09-25T10:00:00+00:00'},
                'ust': [['2025-%02d-%02d' % (1 + i // 28, 1 + i % 28), 4.0] for i in range(250)]}
        calls.clear()

        def gj2(url, headers=None, timeout=30):
            calls.append(url)
            if 'frankfurter' in url:
                raise zd.urllib.error.HTTPError(url, 503, 'Service Unavailable', {}, None)
            return self.BUBA
        with mock.patch.object(zd, 'get_json', side_effect=gj2), mock.patch.object(zd, 'get', side_effect=gt):
            o = zd.build_rynki(prev, today=datetime.date(2026, 9, 26))
        self.assertEqual(o['ok']['fx'], False)
        self.assertEqual(o['fx'], prev['fx']); self.assertEqual(o['part_at']['fx'], '2026-09-25T10:00:00+00:00', 'część z błędem — poprzednia, z własnym czasem')
        self.assertTrue(any(e.startswith('Frankfurter:') for e in zd.META['errors']))
        self.assertFalse(any('field_tdr_date_value=2025' in c for c in calls), 'poprzedni rok z pliku — bez pobierania')
        self.assertEqual(o['ust'][-1], ['2026-09-25', 5.17])
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                zd.build_rynki(None, today=datetime.date(2026, 9, 26))
        zd.META['errors'].clear()


class DzwigniaV104(unittest.TestCase):
    """v104: dźwignia i pozycje w krypto — brak ≠ zero, plik dzienny Binance z dnia wcześniej przy 404, część z błędem = poprzednia wersja
    z własnym czasem, historia dzienna, harmonogram co godzinę. Odpowiedzi to skrócone nagrania prawdziwych (26.09.2026)."""
    HL = [{'universe': [{'szDecimals': 5, 'name': 'BTC', 'maxLeverage': 40}, {'szDecimals': 4, 'name': 'ETH'}, {'szDecimals': 2, 'name': 'SOL'},
                        {'szDecimals': 0, 'name': 'XRP'}, {'name': 'HYPE'}, {'name': 'ZEC'}, {'name': 'OLD', 'isDelisted': True}, {'name': 'BAD'}, {'name': 'NOPX'}]},
          [{'funding': '0.000007236', 'openInterest': '37688.30372', 'prevDayPx': '84179.0', 'dayNtlVlm': '2577312373.15', 'markPx': '83895.0'},
           {'funding': '0.0000125', 'openInterest': '1104443.2964', 'prevDayPx': '2679.0', 'dayNtlVlm': '839906068.22', 'markPx': '2687.37'},
           {'funding': None, 'openInterest': '5683459.5', 'prevDayPx': '116.44', 'dayNtlVlm': '598530337.37', 'markPx': '120.39'},
           {'funding': '0.0000125', 'openInterest': '197101732.0', 'prevDayPx': '0', 'dayNtlVlm': None, 'markPx': '1.5519'},
           {'funding': '0.0000125', 'openInterest': '46925000', 'prevDayPx': '40.5', 'dayNtlVlm': '1', 'markPx': '40.0'},
           {'funding': '0.0000125', 'openInterest': '1000', 'prevDayPx': '1', 'dayNtlVlm': '1', 'markPx': '1.0'},
           {'funding': '0.0000125', 'openInterest': '99999999', 'prevDayPx': '1', 'dayNtlVlm': '1', 'markPx': '100.0'},
           {'funding': '0.0000125', 'openInterest': 'abc', 'prevDayPx': '1', 'dayNtlVlm': '1', 'markPx': '100.0'},
           {'funding': '0.0000125', 'openInterest': '5', 'prevDayPx': '1', 'dayNtlVlm': '1', 'markPx': None}]]
    FH = [{'coin': 'BTC', 'fundingRate': '0.0000125', 'premium': '0.00045', 'time': 1789797600067 + i * 3600000} for i in range(30)]
    CSV = ('create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n'
           '2026-09-24 00:40:00,BTCUSDT,98304.14,8289594909.64,1.30110545,1.90695000,1.15300771,1.58081800\n'
           '2026-09-24 21:10:00,BTCUSDT,95949.275,8088522776.30,1.31978997,1.92520800,1.22623574,\n'
           '2026-09-24 10:00:00,BTCUSDT,97000.0,8100000000.0,1.3,1.9,1.2,1.0\n')
    TS0 = 1790312400000   # 2026-09-25T05:00Z — świece godzinowe do 26.09 05:00 (dzień przebiegu)
    DVOL = {'result': {'data': [[1790312400000 + i * 3600000, 38.0, 38.5, 37.9, 38.0 + i * 0.1] for i in range(25)] + [[1790312400000 + 25 * 3600000, 1, 1, 1, 'x']]}}   # TS0 wprost (zasięg klasy nie sięga do wyrażenia listowego)
    BOOK = {'result': [{'instrument_name': 'BTC-27MAR26-90000-P', 'open_interest': 10.5, 'estimated_delivery_price': 83914.0},
                       {'instrument_name': 'BTC-27MAR26-90000-C', 'open_interest': 4.0, 'estimated_delivery_price': 83900.0},
                       {'instrument_name': 'BTC-25DEC26-100000-C', 'open_interest': 6.0, 'estimated_delivery_price': 84000.0},
                       {'instrument_name': 'BTC-2OCT26-80000-P', 'open_interest': None, 'estimated_delivery_price': 83914.0},
                       {'instrument_name': 'BTC-PERPETUAL', 'open_interest': 999.0}, {'instrument_name': 'BTC-31FEB26-1-P', 'open_interest': 1.0}]}
    OKX_F = {'code': '0', 'data': [{'fundingRate': '0.0000210707400457', 'fundingTime': '1790409600000', 'nextFundingTime': '1790438400000', 'instId': 'BTC-USDT-SWAP'}]}
    OKX_OI = {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP', 'oi': '2855968.27', 'oiCcy': '28559.6827', 'oiUsd': '2395706135.54', 'ts': '1790400586675'}]}
    OKX_LS = {'code': '0', 'data': [['1790265600000', '1.33'], ['1790352000000', '1.36'], ['1790179200000', 'x']]}
    KR = {'result': 'success', 'serverTime': '2026-09-26T07:13:30.453Z', 'tickers': [   # v109: nagranie 26.09 (skrócone) + wiersze zepsute
        {'symbol': 'PI_XBTUSD', 'tag': 'perpetual', 'pair': 'XBT:USD', 'markPrice': 84017.82744462838, 'openInterest': 2301810.0, 'fundingRate': 2.03085217e-10, 'suspended': False},
        {'symbol': 'PF_XBTUSD', 'last': 84002, 'lastTime': '2026-09-26T07:13:21.588672Z', 'tag': 'perpetual', 'pair': 'XBT:USD', 'markPrice': 84005.90053859816, 'vol24h': 3819.3305, 'volumeQuote': 321113985.4452,
         'openInterest': 2175.3188, 'fundingRate': -0.2528880558106387, 'fundingRatePrediction': -0.17626672464625, 'suspended': False, 'indexPrice': 84004.17},
        {'symbol': 'PF_ETHUSD', 'last': 2690.1, 'lastTime': '2026-09-26T07:13:01.505673Z', 'tag': 'perpetual', 'pair': 'ETH:USD', 'markPrice': 2690.15637870521, 'vol24h': 34005.321, 'volumeQuote': 91647373.6034,
         'openInterest': 26150.793, 'fundingRate': 0.008750205298334228, 'fundingRatePrediction': 0.00999986457, 'suspended': False, 'indexPrice': 2690.03},
        {'symbol': 'PF_SOLUSD', 'markPrice': 120.47, 'openInterest': 'abc', 'fundingRate': -0.00187}, {'markPrice': 1.0, 'openInterest': 5.0}, 'śmieć',
        {'symbol': 'PF_XRPUSD', 'suspended': True, 'markPrice': 1.55, 'openInterest': 100.0, 'fundingRate': 0.0}]}
    CB = {'BTC': {'symbol': 'BTC-PERP', 'type': 'PERP', 'base_asset_name': 'BTC', 'quote_asset_name': 'USDC', 'qty_24hr': '43932.0549', 'notional_24hr': '3692548262.26802', 'open_interest': '1075.7102',
                  'funding_interval': '3600000000000', 'trading_state': 'TRADING', 'quote': {'best_bid_price': '84012.9', 'best_ask_price': '84013', 'trade_price': '84011.6', 'index_price': '84001.3', 'mark_price': '84012.9',
                                                                                          'settlement_price': '83994.8', 'predicted_funding': '0.000008', 'timestamp': '2026-09-26T07:13:31.239Z'}},
          'ETH': {'symbol': 'ETH-PERP', 'type': 'PERP', 'base_asset_name': 'ETH', 'quote_asset_name': 'USDC', 'qty_24hr': '948282.0212', 'notional_24hr': '2555737253.816879', 'open_interest': '17069.0224',
                  'funding_interval': '3600000000000', 'trading_state': 'TRADING', 'quote': {'mark_price': '2690', 'index_price': '2689.96', 'predicted_funding': '0.000006', 'timestamp': '2026-09-26T07:13:33.270Z'}}}
    DY = {'BTC': {'markets': {'BTC-USD': {'clobPairId': '0', 'ticker': 'BTC-USD', 'status': 'ACTIVE', 'oraclePrice': '83979.10208', 'priceChange24H': '200.39102', 'volume24H': '2793171.9686', 'trades24H': 1411,
                                          'nextFundingRate': '-0.00000078846153846154', 'openInterest': '190.2829', 'atomicResolution': -10, 'marketType': 'CROSS', 'baseOpenInterest': '782.0931', 'defaultFundingRate1H': '0'}}},
          'ETH': {'markets': {'ETH-USD': {'ticker': 'ETH-USD', 'status': 'ACTIVE', 'oraclePrice': '2688.871112', 'volume24H': '38816886.7794', 'nextFundingRate': '-0.00005016346153846154', 'openInterest': '6080.201',
                                          'baseOpenInterest': '13993.985', 'marketType': 'CROSS'}}}}
    TODAY = datetime.date(2026, 9, 26)

    @classmethod
    def zip_bytes(cls, csv_text, name='BTCUSDT-metrics-2026-09-24.csv'):
        import io, zipfile
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z:
            z.writestr(name, csv_text)
        return b.getvalue()

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    def get_json(self, url, headers=None, timeout=30):
        self.assertLessEqual(timeout, 20, url)
        if 'get_volatility_index_data' in url: return self.DVOL
        if 'get_book_summary_by_currency' in url: return self.BOOK
        if 'funding-rate' in url: return self.OKX_F
        if 'open-interest' in url: return self.OKX_OI
        if 'long-short-account-ratio' in url: return self.OKX_LS
        if 'futures.kraken.com' in url: return self.KR   # v109
        if 'international.coinbase.com' in url: return self.CB[url.split('/instruments/')[1][:3]]
        if 'indexer.dydx.trade' in url: return self.DY[url.split('ticker=')[1][:3]]
        raise AssertionError('nieznany adres ' + url)

    def hl_post(self, body, timeout=20):
        return self.HL if body['type'] == 'metaAndAssetCtxs' else self.FH

    def test_num_never_zero_for_missing(self):
        self.assertEqual(zd.lev_num('1.5'), 1.5); self.assertEqual(zd.lev_num(3), 3.0)
        for v in (None, '', 'abc', True, False, 'nan', 'inf', [1]):
            self.assertIsNone(zd.lev_num(v), repr(v))

    def test_hl_parse_keep_top_and_reject_bad_rows(self):
        o = zd.hl_parse(self.HL, top=3)
        self.assertEqual(o['top'], ['BTC', 'ETH', 'HYPE', 'SOL', 'XRP'], 'trzy największe wg USD + zawsze BTC/ETH/SOL/XRP, kolejność malejąca (ZEC za mały)')
        self.assertEqual(o['n'], 6, 'wiersze bez liczbowego openInterest, bez ceny i wycofane — odrzucone')
        b = o['rows']['BTC']
        self.assertEqual(b['f_y'], round(0.000007236 * 24 * 365 * 100, 3)); self.assertEqual(b['oi_usd'], round(37688.30372 * 83895.0)); self.assertAlmostEqual(b['d1'], (83895.0 / 84179.0 - 1) * 100, 3)
        self.assertIsNone(o['rows']['SOL']['f_h']); self.assertIsNone(o['rows']['SOL']['f_y'], 'brak stawki → None, nie zero')
        self.assertIsNone(o['rows']['XRP']['d1'], 'cena sprzed doby 0 → brak zmiany, nie nieskończoność'); self.assertIsNone(o['rows']['XRP']['vol_usd'])
        with self.assertRaises(ValueError):
            zd.hl_parse([{'universe': []}, [{'a': 1}]])
        with self.assertRaises(ValueError):
            zd.hl_parse({'universe': []})

    def test_hl_fund7_mean_needs_24_rows(self):
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post):
            self.assertEqual(zd.hl_fund7('BTC', 1790400000000), round(0.0000125 * 24 * 365 * 100, 3))
        with mock.patch.object(zd, 'hl_post', return_value=self.FH[:10]):
            self.assertIsNone(zd.hl_fund7('BTC', 1790400000000))

    def test_bn_parse_last_row_and_mean_missing_is_none(self):
        o = zd.bn_parse(self.CSV)
        self.assertEqual(o['t'], '2026-09-24 21:10:00'); self.assertEqual(o['n'], 3)
        self.assertEqual(o['last']['oi'], 95949.275); self.assertEqual(o['last']['ls'], 1.22623574); self.assertIsNone(o['last']['taker'], 'pusta kolumna → None')
        self.assertAlmostEqual(o['mean']['taker'], (1.580818 + 1.0) / 2, 6, 'średnia tylko z liczb'); self.assertAlmostEqual(o['mean']['oi'], (98304.14 + 95949.275 + 97000.0) / 3, 4)
        with self.assertRaises(ValueError):
            zd.bn_parse('create_time,symbol\n')

    def test_bn_404_falls_back_one_day_then_gives_up(self):
        calls = []

        def gb(url, headers=None, timeout=60):
            calls.append(url); self.assertLessEqual(timeout, 20)
            if '2026-09-25' in url:
                raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
            return self.zip_bytes(self.CSV.replace('BTCUSDT', 'ETHUSDT') if 'ETHUSDT' in url else self.CSV)
        with mock.patch.object(zd, 'get_bytes', side_effect=gb):
            o = zd.lev_bn(None, self.TODAY)
        self.assertEqual(o['day'], '2026-09-24'); self.assertEqual(o['BTC']['last']['oi'], 95949.275); self.assertEqual(o['ETH']['n'], 3)
        self.assertEqual([c.split('/')[-1] for c in calls], ['BTCUSDT-metrics-2026-09-25.zip', 'BTCUSDT-metrics-2026-09-24.zip', 'ETHUSDT-metrics-2026-09-24.zip'])
        calls.clear()
        with mock.patch.object(zd, 'get_bytes', side_effect=gb):
            self.assertIsNone(zd.lev_bn(o, self.TODAY), 'ten sam dzień w poprzednim pliku — zostaje poprzednia część')
        self.assertEqual([c.split('/')[-1] for c in calls], ['BTCUSDT-metrics-2026-09-25.zip'], 'tylko jedna próba, czy wczorajszy plik już się pojawił')
        calls.clear()
        with mock.patch.object(zd, 'get_bytes', side_effect=gb):
            self.assertIsNone(zd.lev_bn(dict(o, day='2026-09-25'), self.TODAY))
        self.assertEqual(calls, [], 'wczorajszy dzień już w pliku — bez pobierania')
        with mock.patch.object(zd, 'get_bytes', side_effect=zd.urllib.error.HTTPError('u', 404, 'Not Found', {}, None)):
            with self.assertRaises(ValueError):
                zd.lev_bn({'day': '2026-09-23', 'BTC': {}}, self.TODAY)
        # ten sam dzień, ale ETH wtedy zawiodło (None): ponawiamy tylko ETHUSDT — bez czekania na następny dzień pliku
        calls.clear()
        with mock.patch.object(zd, 'get_bytes', side_effect=gb):
            r = zd.lev_bn({'day': '2026-09-24', 'BTC': o['BTC'], 'ETH': None}, self.TODAY)
        self.assertEqual([c.split('/')[-1] for c in calls], ['BTCUSDT-metrics-2026-09-25.zip', 'ETHUSDT-metrics-2026-09-24.zip'], 'próba wczorajszego pliku, potem tylko ETH z tego samego dnia')
        self.assertEqual((r['day'], r['ETH']['n']), ('2026-09-24', 3)); self.assertIs(r['BTC'], o['BTC'], 'BTC bez ponownego pobierania')
        with mock.patch.object(zd, 'get_bytes', side_effect=zd.urllib.error.HTTPError('u', 404, 'Not Found', {}, None)):
            self.assertIsNone(zd.lev_bn({'day': '2026-09-24', 'BTC': o['BTC'], 'ETH': None}, self.TODAY), 'ETH wciąż 404 → zostaje poprzednia część (BTC), nie zero')
        self.assertTrue(any(e.startswith('Dźwignia: Binance ETHUSDT 2026-09-24') for e in zd.META['errors']), zd.META['errors'])
        with mock.patch.object(zd, 'get_bytes', side_effect=zd.urllib.error.HTTPError('u', 503, 'x', {}, None)):
            with self.assertRaises(zd.urllib.error.HTTPError):
                zd.lev_bn(None, self.TODAY)   # inny błąd niż 404 — bez cofania się o dzień

    def test_deribit_dvol_expiry_and_put_call(self):
        with mock.patch.object(zd, 'get_json', side_effect=self.get_json):
            d = zd.dr_dvol('BTC', self.TS0 + 26 * 3600000)
            self.assertEqual(d['v'], 40.4); self.assertEqual(d['v24'], 38.0); self.assertAlmostEqual(d['d1'], (40.4 / 38.0 - 1) * 100, 3); self.assertEqual(d['t'], '2026-09-26T05:00:00+00:00')
            b = zd.dr_book('BTC')
        self.assertEqual((b['oi'], b['oi_p'], b['oi_c'], b['pc'], b['n']), (20.5, 10.5, 10.0, 1.05, 3), 'PERPETUAL, brak liczby i zła data — pominięte')
        self.assertEqual(b['exp'], [['2026-03-27', 14.5], ['2026-12-25', 6.0]]); self.assertEqual(b['px'], 83914.0)
        self.assertEqual(zd.dr_exp('ETH-2OCT26-2900-C'), ('2026-10-02', 'C')); self.assertEqual(zd.dr_exp('BTC-PERPETUAL'), (None, None)); self.assertEqual(zd.dr_exp('BTC-31FEB26-1-P'), (None, None))
        few = {'result': {'data': [[1790139600000, 1, 1, 1, 38.0]]}}
        with mock.patch.object(zd, 'get_json', return_value=few):
            d = zd.dr_dvol('BTC', 1790139600000)
        self.assertIsNone(d['v24']); self.assertIsNone(d['d1'], 'brak świecy sprzed doby → brak zmiany, nie zero')

    def test_okx_funding_period_from_fields_and_latest_ratio(self):
        with mock.patch.object(zd, 'get_json', side_effect=self.get_json):
            o = zd.lev_okx(None)
        b = o['BTC']
        self.assertEqual(b['f_hours'], 8.0); self.assertEqual(b['f_y'], round(0.0000210707400457 * 3 * 365 * 100, 3)); self.assertEqual(b['oi_usd'], 2395706136)
        self.assertEqual((b['ls'], b['ls_t']), (1.36, '2026-09-25T16:00:00+00:00'), 'najnowszy punkt dzienny z pełnym czasem (wiersz bez liczby pominięty)')
        bad = dict(self.OKX_LS, data=[])
        with mock.patch.object(zd, 'get_json', side_effect=lambda u, headers=None, timeout=30: bad if 'ratio' in u else self.get_json(u, headers, timeout)):
            o = zd.lev_okx({'BTC': {'t': 'old', 'ls': 1.1}})
        self.assertNotIn('ls', o['BTC'], 'pusta odpowiedź — pole pominięte, nie zero'); self.assertEqual(o['BTC']['oi_usd'], 2395706136)
        self.assertTrue(any(e.startswith('Dźwignia: OKX: długie/krótkie BTC') for e in zd.META['errors']), zd.META['errors'])
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('offline')):
            with self.assertRaises(ValueError):
                zd.lev_okx({'BTC': {'t': 'old', 'ls': 1.1}})   # nic nowego — część nieudana, decyzja o zachowaniu poprzedniej zapada wyżej

    def test_build_happy_path_history_and_binance_day(self):
        gb = lambda url, headers=None, timeout=60: self.zip_bytes(self.CSV.replace('BTCUSDT', 'ETHUSDT') if 'ETHUSDT' in url else self.CSV)
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=gb), mock.patch.object(zd, 'get_json', side_effect=self.get_json):
            o = zd.build_dzwignia(None, today=self.TODAY)
        self.assertEqual(o['ok'], {k: True for k in ('hl', 'bn', 'dr', 'okx', 'kr', 'cb', 'dy')}); self.assertEqual(o['part_at'], {k: zd.NOW for k in ('hl', 'bn', 'dr', 'okx', 'kr', 'cb', 'dy')})
        self.assertEqual(o['hl']['f7_y'], {'BTC': round(0.0000125 * 24 * 365 * 100, 3), 'ETH': round(0.0000125 * 24 * 365 * 100, 3)})
        self.assertEqual(o['bn']['day'], '2026-09-25'); self.assertEqual(o['dr']['ETH']['opt']['pc'], 1.05); self.assertEqual(o['okx']['ETH']['ls'], 1.36)
        self.assertEqual([h['d'] for h in o['hist']], ['2026-09-25', '2026-09-26'], 'liczby Binance w dniu pliku, reszta w dniu przebiegu')
        self.assertEqual(o['hist'][0], {'d': '2026-09-25', 'bn_btc': 8088522776.3, 'bn_eth': 8088522776.3})
        self.assertEqual(o['hist'][1]['hl_btc'], round(37688.30372 * 83895.0)); self.assertEqual(o['hist'][1]['dvol_eth'], 40.4); self.assertNotIn('bn_btc', o['hist'][1])
        self.assertEqual(zd.META['errors'], [])
        # drugi przebieg dzień później: nowy wpis historii, dzisiejszy nadpisany, poprzednie zachowane; historia przycięta do LEV_HIST
        prev = dict(o, hist=o['hist'] + [{'d': '2026-09-%02d' % d, 'hl_btc': 1} for d in range(1, 25)] + [{'d': '2030-01-01', 'hl_btc': 5}])
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=gb), mock.patch.object(zd, 'get_json', side_effect=self.get_json), mock.patch.object(zd, 'LEV_HIST', 10):
            o2 = zd.build_dzwignia(prev, today=datetime.date(2026, 9, 27))
        self.assertEqual(len(o2['hist']), 10); self.assertEqual(o2['hist'][-1]['d'], '2026-09-27'); self.assertNotIn('2030-01-01', [h['d'] for h in o2['hist']], 'wpis z przyszłości odrzucony')
        self.assertEqual(o2['hist'][-2]['bn_btc'], 8088522776.3, 'plik Binance z 26.09 zapisany w dniu 26.09')

    def test_build_partial_failure_keeps_previous_part_with_its_time(self):
        prev = {'at': '2026-09-25T10:00:00+00:00', 'part_at': {'dr': '2026-09-25T09:00:00+00:00', 'okx': '2026-09-25T10:00:00+00:00'},
                'ok': {'hl': True, 'bn': True, 'dr': True, 'okx': True}, 'dr': {'BTC': {'dvol': {'v': 30.0, 't': '2026-09-25T09:00:00+00:00'}}}, 'okx': {'BTC': {'t': 'old', 'ls': 1.2}},
                'bn': {'day': '2026-09-25', 'BTC': {'last': {'oi_usd': 1.0}}, 'ETH': {'last': {'oi_usd': 1.0}}}, 'hist': [{'d': '2026-09-25', 'hl_btc': 3e9}]}   # obie monety z tego dnia — bez pobierania

        def gj(url, headers=None, timeout=30):
            if 'deribit' in url:
                raise zd.urllib.error.HTTPError(url, 503, 'Service Unavailable', {}, None)
            return self.get_json(url, headers, timeout)
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('nie powinno pobierać')), mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.build_dzwignia(prev, today=self.TODAY)
        self.assertEqual(o['ok'], {'hl': True, 'bn': True, 'dr': False, 'okx': True, 'kr': True, 'cb': True, 'dy': True})
        self.assertEqual(o['dr'], prev['dr']); self.assertEqual(o['part_at']['dr'], '2026-09-25T09:00:00+00:00', 'część z błędem — poprzednia, z własnym czasem')
        self.assertEqual(o['bn'], prev['bn']); self.assertEqual(o['part_at']['bn'], '2026-09-25T10:00:00+00:00', 'ten sam dzień pliku — poprzednia część, czas pliku (brak part_at) ')
        self.assertTrue(any(e.startswith('Dźwignia: Deribit: DVOL BTC: HTTP Error 503') for e in zd.META['errors']), zd.META['errors'])
        self.assertNotIn('dvol_btc', o['hist'][-1], 'część zachowana z poprzedniego przebiegu nie trafia do historii jako dzisiejsza')
        self.assertEqual(o['hist'][-1]['hl_btc'], round(37688.30372 * 83895.0))
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'hl_post', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                zd.build_dzwignia(None, today=self.TODAY)
            o = zd.build_dzwignia(prev, today=self.TODAY)   # wszystko padło, ale jest poprzedni plik: części zachowane, bn z tym samym dniem
        self.assertEqual(o['ok'], {'hl': False, 'bn': True, 'dr': False, 'okx': False, 'kr': False, 'cb': False, 'dy': False}); self.assertEqual(o['okx'], prev['okx'])
        self.assertEqual([e[:22] for e in zd.META['errors']][-6:], ['Dźwignia: Hyperliquid:', 'Dźwignia: Deribit: DVO', 'Dźwignia: OKX: finanso', 'Dźwignia: Kraken: offl', 'Dźwignia: Coinbase: BT', 'Dźwignia: dYdX: BTC: o'])

    def test_build_only_failed_parts_keeps_healthy_with_their_time(self):
        t0 = '2026-09-26T05:00:00+00:00'
        prev = {'at': '2026-09-26T05:20:00+00:00', 'full_at': t0, 'ok': {'hl': True, 'bn': True, 'dr': False, 'okx': True},
                'part_at': {'hl': t0, 'bn': '2026-09-25T06:00:00+00:00', 'okx': t0, 'dr': '2026-09-24T05:00:00+00:00'},
                'hl': {'rows': {'BTC': {'oi_usd': 3e9, 'f_y': 8.0}}, 'top': ['BTC']}, 'bn': {'day': '2026-09-25', 'BTC': {'last': {'oi_usd': 8e9}}}, 'okx': {'BTC': {'t': t0, 'ls': 1.2}},
                'dr': {'BTC': {'dvol': {'v': 30.0, 't': '2026-09-24T05:00:00+00:00'}}}, 'hist': [{'d': '2026-09-26', 'hl_btc': 3e9, 'f_btc': 8.0}]}
        def gj(u, headers=None, timeout=30):
            self.assertIn('deribit', u, 'zdrowej części nie pobieramy'); return self.get_json(u, headers, timeout)
        with mock.patch.object(zd, 'hl_post', side_effect=AssertionError('zdrowej części nie pobieramy')), mock.patch.object(zd, 'get_bytes', side_effect=AssertionError('zdrowej części nie pobieramy')), \
             mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.build_dzwignia(prev, today=self.TODAY, only={'dr'})
        self.assertEqual(o['ok'], {'hl': True, 'bn': True, 'dr': True, 'okx': True, 'kr': False, 'cb': False, 'dy': False}, 'plik sprzed v109: nowych części nie ma w poprzednim, a `only` ich nie obejmuje')
        self.assertEqual(o['part_at'], {'hl': t0, 'bn': '2026-09-25T06:00:00+00:00', 'okx': t0, 'dr': zd.NOW}, 'zdrowe części z własnym czasem, dobrana część z czasem przebiegu')
        self.assertEqual((o['hl'], o['okx'], o['bn']), (prev['hl'], prev['okx'], prev['bn'])); self.assertEqual(o['dr']['BTC']['dvol']['v'], 40.4)
        self.assertEqual(o['full_at'], t0, 'dobranie części nie odświeża czasu pełnej budowy'); self.assertEqual(o['at'], zd.NOW)
        self.assertEqual(o['hist'][-1]['hl_btc'], 3e9, 'wpis dnia z pełnej budowy zostaje'); self.assertEqual(o['hist'][-1]['dvol_btc'], 40.4, 'dobrana część dopisuje swoją liczbę')
        self.assertEqual(zd.META['errors'], [])
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_json', side_effect=self.get_json):
            f = zd.build_dzwignia(prev, today=self.TODAY)
        self.assertEqual(f['full_at'], zd.NOW, 'pełna budowa ustawia full_at'); self.assertEqual(f['part_at']['hl'], zd.NOW)
        with mock.patch.object(zd, 'hl_post', side_effect=self.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_json', side_effect=self.get_json):
            g = zd.build_dzwignia(dict(prev, ok={'hl': True, 'bn': True, 'dr': False, 'okx': False}, okx=None), today=self.TODAY, only={'dr', 'okx'})
        self.assertEqual(g['ok'], {'hl': True, 'bn': True, 'dr': True, 'okx': True, 'kr': False, 'cb': False, 'dy': False}, 'dobierane obie brakujące części'); self.assertEqual(g['okx']['BTC']['ls'], 1.36)

    def test_main_schedule_hourly_and_fallback(self):
        saved = {}
        stubs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_stres', 'build_wieloryby', 'build_indeksy', 'build_ceny_krypto', 'build_insider')]
        env = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY', 'SITE_URL', 'CACHE_DIR')}
        fresh_prev = {'at': _iso(10), 'ok': {'hl': True, 'bn': True, 'dr': True, 'okx': True, 'kr': True, 'cb': True, 'dy': True}, 'hl': {'rows': {}}}
        stale_prev = {'at': _iso(10), 'ok': {'hl': True, 'bn': False, 'dr': True, 'okx': True, 'kr': True, 'cb': True, 'dy': True}, 'hl': {'rows': {}}}
        old_prev = {'at': _iso(10), 'full_at': _iso(70), 'ok': {'hl': True, 'bn': False, 'dr': True, 'okx': True, 'kr': True, 'cb': True, 'dy': True}, 'hl': {'rows': {}}}   # dobierany co 20 min, pełna budowa sprzed godziny
        built = {'at': zd.NOW, 'ok': {'hl': True, 'bn': True, 'dr': False, 'okx': True}}
        [p.start() for p in stubs]
        try:
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                 mock.patch.object(zd, 'previous', lambda name: fresh_prev if name == 'dzwignia' else None), mock.patch.object(zd, 'build_dzwignia', side_effect=AssertionError('nie powinien budować')):
                zd.main()
            self.assertIs(saved['dzwignia'], fresh_prev); self.assertEqual(zd.META['ok']['dzwignia'], 'cached'); self.assertEqual(zd.META['ok']['dzwignia_bn'], 'cached')
            calls = []
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                 mock.patch.object(zd, 'previous', lambda name: stale_prev if name == 'dzwignia' else None), mock.patch.object(zd, 'build_dzwignia', side_effect=lambda p, only=None: calls.append((p, only)) or built):
                zd.main()
            self.assertEqual(calls, [(stale_prev, {'bn'})], 'młody plik z częścią z błędem — dobierana tylko ta część (z poprzednim jako wejście)')
            self.assertIs(saved['dzwignia'], built); self.assertIs(zd.META['ok']['dzwignia'], True); self.assertIs(zd.META['ok']['dzwignia_dr'], False); self.assertIs(zd.META['ok']['dzwignia_hl'], True)
            calls.clear()
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                 mock.patch.object(zd, 'previous', lambda name: old_prev if name == 'dzwignia' else None), mock.patch.object(zd, 'build_dzwignia', side_effect=lambda p, only=None: calls.append((p, only)) or built):
                zd.main()
            self.assertEqual(calls, [(old_prev, None)], 'pełna budowa sprzed godziny (full_at) — budowane wszystko, choć `at` jest młode')
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                 mock.patch.object(zd, 'previous', lambda name: stale_prev if name == 'dzwignia' else None), mock.patch.object(zd, 'build_dzwignia', side_effect=RuntimeError('offline')):
                zd.main()
            self.assertIs(saved['dzwignia'], stale_prev, 'awaria budowy — zostaje poprzedni plik'); self.assertIs(zd.META['ok']['dzwignia'], False)
            self.assertIn('Dźwignia: offline', zd.META['errors'])
        finally:
            [p.stop() for p in stubs]

    def test_page_reads_the_file(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'), encoding='utf-8').read()
        self.assertIn("srvJSON('dzwignia')", html); self.assertIn('id="c-dzwignia"', html)
        self.assertEqual(zd.LEV_EVERY, 55); self.assertEqual(zd.LEV_TIMEOUT, 20)
        self.assertIn("levAsOf(levAt('hl'))", html); self.assertIn("levAsOf(o.t||levAt('dr'))", html)


class WielorybyV105(unittest.TestCase):
    """v105: portfele giełd na Ethereum — dekodowanie zdarzeń, salda (brak ≠ zero), historia dobowa, paczki bloków,
    pełny przebieg na nagraniach, część z błędem = poprzednia wersja z własnym czasem."""
    T = zd.WH_TRANSFER
    B1, B2 = '0xbe0eb53f46cd790cd13851d5eff43d12404d33e8', '0xf977814e90da44bfa03b6295a0616a897441acec'
    O1 = '0xa073345811e360e9b66f24bc11f3a4bfa924f236'
    X = '0x1111111111111111111111111111111111111111'

    @staticmethod
    def _w(n):
        return '0x' + '0' * 24 + n[2:]

    @staticmethod
    def _h(v, w=64):
        return '0x' + format(int(v), 'x').rjust(w, '0')

    def _log(self, tok, fr, to, amt, blk=100, tx='0x' + 'ab' * 32, li=1, data=None):
        return {'address': tok, 'topics': [self.T, self._w(fr), self._w(to)], 'data': data if data is not None else self._h(amt * 10 ** 6),
                'blockNumber': hex(blk), 'transactionHash': tx, 'logIndex': hex(li)}

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear()
        self.W = {w['addr']: w['exch'] for w in zd.wh_portfele()}

    def test_hex_and_topic(self):
        self.assertEqual(zd.wh_hex('0x1a'), 26); self.assertEqual(zd.wh_hex('0x0'), 0)
        self.assertIsNone(zd.wh_hex('0x')); self.assertIsNone(zd.wh_hex(None)); self.assertIsNone(zd.wh_hex('zz')); self.assertIsNone(zd.wh_hex(12))
        self.assertEqual(zd.wh_topic('0xBE0EB53F46CD790CD13851D5EFF43D12404D33E8'), '0x000000000000000000000000be0eb53f46cd790cd13851d5eff43d12404d33e8')
        self.assertEqual(len(zd.wh_portfele()), 164); self.assertTrue(all(w['src'] and w['since'] and w['addr'] == w['addr'].lower() for w in zd.wh_portfele()))   # v108: 9 + 10 + 108 + 33 + 4

    def test_zakres(self):
        p, luka = zd.wh_zakres(10000, None)
        self.assertTrue(luka); self.assertEqual(p, [(5201, 6000), (6001, 6800), (6801, 7600), (7601, 8400), (8401, 9200), (9201, 10000)], 'pierwszy przebieg: 4800 bloków w 6 paczkach po 800')
        p, luka = zd.wh_zakres(10000, 9900)
        self.assertFalse(luka); self.assertEqual(p, [(9901, 10000)])
        self.assertEqual(zd.wh_zakres(10000, 10000), ([], False)); self.assertEqual(zd.wh_zakres(10000, 10005), ([], False))
        p, luka = zd.wh_zakres(10000, 1000)
        self.assertTrue(luka); self.assertEqual(p[0], (5201, 6000), 'zaległość większa niż 4800 bloków — od nowa, z luką')
        p, luka = zd.wh_zakres(10000, 5200)
        self.assertTrue(luka is False and p[0] == (5201, 6000) and len(p) == 6)
        p, luka = zd.wh_zakres(10000, 3000, start=10000)
        self.assertTrue(luka is False and p[0] == (3001, 3800) and p[-1] == (7001, 7800) and len(p) == 6, 'najwyżej 6 paczek; reszta w następnym przebiegu')
        p, luka = zd.wh_zakres(10000, 5100)
        self.assertTrue(luka and p[0] == (5201, 6000), 'zaległość 4900 > 4800 bloków — z luką')

    def test_dekoduj(self):
        L = [self._log(zd.WH_USDT, self.X, self.B1, 2_000_000, tx='0x' + '01' * 32),           # na giełdę
             self._log(zd.WH_USDC, self.B2, self.X, 1_000_000, tx='0x' + '02' * 32),           # z giełdy (dokładnie próg)
             self._log(zd.WH_USDT, self.B1, self.B2, 9_000_000, tx='0x' + '03' * 32),          # wewnętrzny Binance → pominięty
             self._log(zd.WH_USDT, self.X, self.B1, 999_999, tx='0x' + '04' * 32),             # poniżej progu
             self._log('0x' + 'ee' * 20, self.X, self.B1, 5_000_000, tx='0x' + '05' * 32),      # inny kontrakt
             self._log(zd.WH_USDT, self.X, self.B1, 0, tx='0x' + '06' * 32, data='0x'),        # bez kwoty — nie zero
             self._log(zd.WH_USDC, self.B1, self.O1, 3_000_000, tx='0x' + '07' * 32),          # Binance → OKX: dwa wiersze
             'śmieć', {'address': zd.WH_USDT, 'topics': [self.T]}]
        R = zd.wh_dekoduj(L, self.W)
        rows = sorted(R.values(), key=lambda r: (r['tx'], r['dir']))
        self.assertEqual([(r['token'], r['amt'], r['dir'], r['exch']) for r in rows],
                         [('USDT', 2000000.0, 'in', 'Binance'), ('USDC', 1000000.0, 'out', 'Binance'), ('USDC', 3000000.0, 'in', 'OKX'), ('USDC', 3000000.0, 'out', 'Binance')])
        self.assertTrue(all(r['blk'] == 100 and r['li'] == 1 and r['t'] is None for r in rows))
        self.assertEqual(zd.wh_dekoduj(None, self.W), {}); self.assertEqual(zd.wh_dekoduj([], self.W), {})

    def test_cena(self):
        now = 1_790_400_000
        w = lambda ans, upd: '0x' + format(1, 'x').rjust(64, '0') + format(ans, 'x').rjust(64, '0') + format(now - 100, 'x').rjust(64, '0') + format(upd, 'x').rjust(64, '0') + format(1, 'x').rjust(64, '0')
        self.assertAlmostEqual(zd.wh_cena(w(268886950310, now - 1000), now), 2688.8695031)
        self.assertIsNone(zd.wh_cena(w(268886950310, now - 90000), now), 'odczyt starszy niż doba — brak')
        self.assertIsNone(zd.wh_cena(w(0, now - 10), now)); self.assertIsNone(zd.wh_cena('0x12', now)); self.assertIsNone(zd.wh_cena(None, now))

    def test_hist(self):
        prev = {'Binance': [['2026-09-%02d' % d, '2026-09-%02dT00:05:00+00:00' % d, 1.0, 2.0, 3.0] for d in range(1, 26)] + ['x', [1]], 'Stara': [['2026-09-25', 't', 1, 1, 1]]}
        h = zd.wh_hist(prev, {'Binance': {'eth': 5.0, 'usdt': 6.0, 'usdc': 7.0}, 'OKX': {'eth': 0.5, 'usdt': 0.0, 'usdc': 9.0}}, '2026-09-26', '2026-09-26T08:00:00+00:00')
        self.assertEqual(h['Binance'][-1], ['2026-09-26', '2026-09-26T08:00:00+00:00', 5.0, 6.0, 7.0]); self.assertEqual(len(h['Binance']), 26)
        self.assertEqual(h['OKX'], [['2026-09-26', '2026-09-26T08:00:00+00:00', 0.5, 0.0, 9.0]]); self.assertEqual(h['Stara'], [['2026-09-25', 't', 1, 1, 1]], 'giełda bez świeżego salda zachowuje historię')
        h2 = zd.wh_hist(h, {'Binance': {'eth': 9.0, 'usdt': 9.0, 'usdc': 9.0}}, '2026-09-26', '2026-09-26T12:00:00+00:00')
        self.assertEqual(h2['Binance'][-1][1], '2026-09-26T08:00:00+00:00', 'jeden zrzut na dobę — pierwszy zostaje')
        big = {'Binance': [['2025-%02d-%02d' % (1 + i // 28, 1 + i % 28), 't', 1, 1, 1] for i in range(150)]}
        self.assertEqual(len(zd.wh_hist(big, {'Binance': {'eth': 1, 'usdt': 1, 'usdc': 1}}, '2026-09-26', 't')['Binance']), 120)

    def test_polacz(self):
        a = {'tx': '0x1', 'li': 1, 'exch': 'Binance', 'dir': 'in', 'amt': 1e6, 'blk': 50}
        b = {'tx': '0x2', 'li': 2, 'exch': 'Binance', 'dir': 'in', 'amt': 5e6, 'blk': 120}
        c = dict(b, amt=5e6, t='nowy')
        d = {'tx': '0x3', 'li': 0, 'exch': 'OKX', 'dir': 'out', 'amt': 2e6, 'blk': 130}
        r = zd.wh_polacz([a, b], [c, d, 'x', {'blk': 'a', 'amt': 1}], 100)
        self.assertEqual([(x['tx'], x['amt']) for x in r], [('0x2', 5e6), ('0x3', 2e6)], 'poza oknem odpada, duplikat raz, malejąco wg kwoty')
        self.assertEqual(r[0].get('t'), 'nowy')
        self.assertEqual(len(zd.wh_polacz([], [dict(d, tx=str(i), amt=i) for i in range(100)], 0, maks=60)), 60)

    # ---- pełny przebieg na nagraniach: jedno żądanie zbiorcze sald, po dwa zapytania o logi na paczkę, czasy bloków
    def _rpc(self, head=10000, fail=None, bal=None, price_ok=True):
        calls = []
        now = 1_790_400_000

        def post(url, body, timeout=60):
            calls.append((url, [b['method'] for b in body]))
            if fail and fail(url, body):
                raise zd.urllib.error.HTTPError(url, 403, 'Forbidden', {}, None)
            out = []
            for b in body:
                m, p = b['method'], b['params']
                if m == 'eth_getBlockByNumber':
                    n = head if p[0] == 'latest' else int(p[0], 16)
                    r = {'number': hex(n), 'timestamp': hex(now - (head - n) * 12)}
                elif m == 'eth_getBalance':
                    r = bal(p[0], 'eth') if bal else self._h(int(1.5e18), 1)
                elif m == 'eth_call' and p[0]['to'] == zd.WH_CHAINLINK:
                    r = ('0x' + '1'.rjust(64, '0') + format(268886950310, 'x').rjust(64, '0') + format(now - 200, 'x').rjust(64, '0')
                         + format(now - 100 if price_ok else now - 100000, 'x').rjust(64, '0') + '1'.rjust(64, '0'))
                elif m == 'eth_call':
                    tok = 'usdt' if p[0]['to'] == zd.WH_USDT else 'usdc'
                    r = bal('0x' + p[0]['data'][-40:], tok) if bal else self._h(2_000_000 * 10 ** 6)
                elif m == 'eth_getLogs':
                    a, z = int(p[0]['fromBlock'], 16), int(p[0]['toBlock'], 16)
                    to_side = p[0]['topics'][1] is None
                    r = []
                    if a <= 9950 <= z and to_side:
                        r.append(self._log(zd.WH_USDT, self.X, self.B1, 2_500_000, blk=9950, tx='0x' + 'aa' * 32, li=3))
                    if a <= 9960 <= z and not to_side:
                        r.append(self._log(zd.WH_USDC, self.O1, self.X, 1_200_000, blk=9960, tx='0x' + 'bb' * 32, li=7))
                        r.append(self._log(zd.WH_USDT, self.B1, self.B2, 8_000_000, blk=9960, tx='0x' + 'cc' * 32, li=8))   # wewnętrzny
                    if a <= 6000 <= z and to_side:
                        r.append(self._log(zd.WH_USDT, self.X, self.B1, 700_000, blk=6000, tx='0x' + 'dd' * 32, li=1))   # poniżej progu
                    if a <= 9970 <= z and not to_side:   # para: ta sama kwota z OKX (9970) i na OKX (9975) — ruch przez nieogłoszony portfel
                        r.append(self._log(zd.WH_USDC, self.O1, self.X, 5_000_000, blk=9970, tx='0x' + 'ee' * 32, li=2))
                    if a <= 9975 <= z and to_side:
                        r.append(self._log(zd.WH_USDC, self.X, self.O1, 5_000_000, blk=9975, tx='0x' + 'ff' * 32, li=4))
                else:
                    raise AssertionError('nieznana metoda ' + m)
                out.append({'jsonrpc': '2.0', 'id': b['id'], 'result': r})
            return out
        return post, calls

    def test_build_happy_path(self):
        post, calls = self._rpc()
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez zapasu kursu')):
            o = zd.build_wieloryby(None)
        self.assertEqual(o['ok'], {'cena': True, 'salda': True, 'transfery': True}); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(sorted(o['part_at']), ['salda', 'transfery']); self.assertEqual(o['blk'], 10000)
        NB = -(-(2 + 3 * len(zd.wh_portfele())) // zd.WH_BATCH)   # v108: 494 wywołań w paczkach po 40 = 13 żądań
        self.assertEqual(NB, 13); self.assertEqual(len(calls), NB + 6 + 1, 'trzynaście paczek sald (≤ 40 wywołań), sześć paczek logów, jedna o czasy bloków')
        self.assertEqual(calls[0][1][:2], ['eth_getBlockByNumber', 'eth_call']); self.assertEqual(sum(len(c[1]) for c in calls[:NB]), 2 + 3 * 164); self.assertEqual(len(calls[0][1]), 40)
        self.assertTrue(all(u == zd.WH_RPC for u, _ in calls))
        self.assertAlmostEqual(o['eth_usd'], 2688.8695031); self.assertEqual(o['eth_usd_at'], o['blk_t'])
        b = o['salda']['Binance']
        self.assertAlmostEqual(b['eth'], 13.5); self.assertAlmostEqual(b['usdt'], 18_000_000); self.assertAlmostEqual(b['usdc'], 18_000_000)
        self.assertAlmostEqual(b['usd'], 13.5 * 2688.8695031 + 36_000_000, places=2); self.assertEqual((b['blk'], b['n']), (10000, 9))
        self.assertEqual(o['salda']['OKX']['n'], 10); self.assertAlmostEqual(o['salda']['OKX']['eth'], 15.0)
        self.assertEqual(o['hist']['Binance'], [[o['blk_t'][:10], o['blk_t'], 13.5, 18000000.0, 18000000.0]])
        self.assertEqual([(r['tx'][:4], r['amt'], r['dir'], r['exch'], r['blk']) for r in o['transfery']],
                         [('0xff', 5000000.0, 'in', 'OKX', 9975), ('0xee', 5000000.0, 'out', 'OKX', 9970), ('0xaa', 2500000.0, 'in', 'Binance', 9950), ('0xbb', 1200000.0, 'out', 'OKX', 9960)],
                         'wewnętrzny i poniżej progu pominięte; para zostaje')
        self.assertEqual([r.get('wew') for r in o['transfery']], [True, True, None, None], 'para „ta sama kwota w obie strony w 5 bloków” oznaczona wew, pozostałe bez klucza')
        self.assertTrue(all(r['t'].endswith('+00:00') for r in o['transfery']))
        self.assertEqual((o['ostatni_blok'], o['okno_od'], o['okno'], o['luka']), (10000, 5201, 4800, False))
        self.assertTrue(o['okno_od_t'] < o['ostatni_t'] == o['blk_t']); self.assertEqual(o['gieldy']['OKX']['tokeny'], ['USDC'])
        self.assertEqual(len(o['wallets']), 164); self.assertTrue(all(w['src'] for w in o['wallets']))
        # drugi przebieg: od ostatniego bloku, jedna paczka, historia bez drugiego zrzutu tego dnia, okno ≈ 24 h w blokach
        post2, calls2 = self._rpc(head=10100)
        with mock.patch.object(zd, 'post_json', side_effect=post2):
            o2 = zd.build_wieloryby(o)
        self.assertEqual(len(calls2), NB + 1, 'salda (trzynaście paczek) + jedna paczka logów; czasy bloków znane z poprzedniego pliku')
        self.assertEqual(calls2[NB][1], ['eth_getLogs', 'eth_getLogs'], 'jedna grupa tematów (164 ≤ WH_TOPICS) = dwa zapytania o logi')
        self.assertEqual((o2['ostatni_blok'], o2['okno_od'], o2['okno']), (10100, 5201, 4900)); self.assertEqual(len(o2['hist']['Binance']), 1)
        self.assertEqual([r['tx'][:4] for r in o2['transfery']], ['0xff', '0xee', '0xaa', '0xbb'], 'wiersze z poprzedniego pliku zostają w oknie')
        self.assertEqual([r.get('wew') for r in o2['transfery']], [True, True, None, None], 'oznaczenie pary zostaje w kolejnym przebiegu')
        with mock.patch.object(zd, 'post_json', side_effect=self._rpc(head=13000)[0]):
            o3 = zd.build_wieloryby(o2)
        self.assertEqual((o3['ostatni_blok'], o3['okno_od'], o3['okno'], o3['luka']), (13000, 5801, 7200, False), 'okno 7200 bloków przesuwa początek'); self.assertEqual(len(o3['transfery']), 4, 'wiersze w oknie zostają')
        with mock.patch.object(zd, 'post_json', side_effect=self._rpc(head=18500)[0]):
            o4 = zd.build_wieloryby(o3)
        self.assertEqual((o4['ostatni_blok'], o4['okno_od'], o4['okno'], o4['luka']), (18500, 13701, 4800, True), 'zaległość 5500 > 4800 bloków: od nowa, z luką'); self.assertEqual(o4['transfery'], [], 'wiersze sprzed okna odpadają')

    def test_build_partial_failures_keep_previous_part(self):
        post, calls = self._rpc()
        with mock.patch.object(zd, 'post_json', side_effect=post):
            prev = zd.build_wieloryby(None)
        prev['at'] = '2026-09-26T07:00:00+00:00'; prev['part_at'] = {'salda': '2026-09-26T07:00:00+00:00', 'transfery': '2026-09-26T06:40:00+00:00'}
        # logi zawodzą (główny i zapas) — salda świeże, transfery poprzednie z własnym czasem; okno z poprzedniego pliku
        post2, calls2 = self._rpc(head=10100, fail=lambda u, b: b[0]['method'] == 'eth_getLogs')
        with mock.patch.object(zd, 'post_json', side_effect=post2), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_wieloryby(prev)
        self.assertEqual(o['ok'], {'cena': True, 'salda': True, 'transfery': False})
        self.assertEqual(o['part_at']['transfery'], '2026-09-26T06:40:00+00:00'); self.assertIs(o['transfery'], prev['transfery'])
        self.assertEqual((o['ostatni_blok'], o['okno'], o['okno_od_t']), (prev['ostatni_blok'], prev['okno'], prev['okno_od_t']))
        self.assertEqual(o['salda']['Binance']['blk'], 10100); self.assertEqual(o['part_at']['salda'], zd.NOW)
        self.assertEqual(len(zd.META['errors']), 1); self.assertTrue(zd.META['errors'][0].startswith('Wieloryby: transfery: węzeł RPC'))
        self.assertEqual([u for u, _ in calls2 if 'flashbots' in u], ['https://rpc.flashbots.net'], 'zapas logów próbowany raz, po dwóch próbach głównego')
        # salda zawodzą — poprzednie salda i historia z własnym czasem, transfery świeże
        zd.META['errors'].clear()
        post3, _ = self._rpc(head=10100, fail=lambda u, b: b[0]['method'] == 'eth_getBlockByNumber' and len(b) > 1)
        with mock.patch.object(zd, 'post_json', side_effect=post3), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_wieloryby(prev)
        self.assertEqual(o['ok'], {'cena': False, 'salda': False, 'transfery': True})
        self.assertIs(o['salda'], prev['salda']); self.assertEqual(o['part_at']['salda'], '2026-09-26T07:00:00+00:00'); self.assertEqual(o['hist'], prev['hist'])
        self.assertEqual((o['eth_usd'], o['eth_usd_at']), (prev['eth_usd'], prev['eth_usd_at']), 'poprzednie salda = poprzedni kurs z jego datą (sumy w USD liczono nim), nie None')
        self.assertEqual(o['ostatni_blok'], 10100)
        self.assertTrue(zd.META['errors'][0].startswith('Wieloryby: salda: węzeł RPC'))
        # wszystko zawodzi — wyjątek (main zachowuje poprzedni plik)
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'post_json', side_effect=RuntimeError('offline')), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_wieloryby(prev)

    def test_missing_balance_is_not_zero_and_missing_price_is_none(self):
        bad = lambda a, tok: '0x' if (a == self.O1 and tok == 'usdc') else self._h(10 ** 6)   # jeden portfel OKX bez odpowiedzi
        post, _ = self._rpc(bal=bad, price_ok=False)
        prev = {'at': '2026-09-25T00:00:00+00:00', 'salda': {'OKX': {'eth': 1.0, 'usdt': 2.0, 'usdc': 3.0, 'usd': 9.0, 'blk': 1, 't': '2026-09-25T00:00:00+00:00', 'n': 10}},
                'hist': {'OKX': [['2026-09-25', '2026-09-25T00:00:00+00:00', 1.0, 2.0, 3.0]]}}
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('brak zapasu')):
            o = zd.build_wieloryby(prev)
        self.assertEqual(o['salda']['OKX'], prev['salda']['OKX'], 'giełda z portfelem bez odpowiedzi: poprzednie saldo z własnym czasem, nie suma częściowa')
        self.assertEqual(o['ok']['salda'], False); self.assertEqual(o['ok']['cena'], False); self.assertIsNone(o['eth_usd'])
        self.assertIsNone(o['salda']['Binance']['usd'], 'bez kursu suma w USD = None'); self.assertAlmostEqual(o['salda']['Binance']['usdt'], 9.0)
        self.assertEqual(len(o['hist']['OKX']), 1, 'bez świeżego salda — bez nowego zrzutu'); self.assertEqual(len(o['hist']['Binance']), 1)
        self.assertEqual(len(zd.META['errors']), 1); self.assertIn('kurs ETH/USD', zd.META['errors'][0]); self.assertIn('salda OKX', zd.META['errors'][0])

    def test_rpc_retries_then_fallback(self):
        seen = []

        def post(url, body, timeout=60):
            seen.append(url)
            if url == zd.WH_RPC:
                raise zd.urllib.error.HTTPError(url, 403, 'Forbidden', {}, None)
            return [{'jsonrpc': '2.0', 'id': b['id'], 'result': '0x1'} for b in body]
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd.time, 'sleep') as sl:
            r = zd.wh_rpc([('eth_getBalance', ['0x1', 'latest'])] * 30)
        self.assertEqual(r, ['0x1'] * 30); self.assertEqual(seen, [zd.WH_RPC, zd.WH_RPC, 'https://1rpc.io/eth', 'https://1rpc.io/eth'], 'zapas w paczkach po 25')
        self.assertEqual(sl.call_count, 2)
        with mock.patch.object(zd, 'post_json', return_value=[{'jsonrpc': '2.0', 'id': 0, 'error': {'code': -32602, 'message': 'Archive requests require a personal token'}}]), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError) as cm:
                zd.wh_rpc([('eth_getLogs', [{}])], kind='logi')
        self.assertIn('Archive', str(cm.exception))
        with mock.patch.object(zd, 'post_json', return_value=[{'jsonrpc': '2.0', 'id': 0, 'result': None}, {'jsonrpc': '2.0', 'id': 1, 'result': '0x2'}]):
            self.assertEqual(zd.wh_rpc([('a', []), ('b', [])]), [None, '0x2'], 'wyniki w kolejności żądań')

    def test_timeout_budget_bounds_the_run(self):
        """Milczący węzeł (każde żądanie kończy się przekroczeniem czasu): łącznie ≤ WH_LIMIT s i dwa żądania, nie 6 × 20 s;
        obie części z poprzedniego pliku, wyjątek dla main() (poprzedni plik zostaje)."""
        clock, calls = [1000.0], []

        def post(url, body, timeout=60):
            calls.append((url, body[0]['method'], timeout)); clock[0] += timeout; raise TimeoutError('timed out')
        prev = {'at': '2026-09-26T07:00:00+00:00', 'part_at': {'salda': '2026-09-26T07:00:00+00:00', 'transfery': '2026-09-26T06:40:00+00:00'},
                'salda': {'Binance': {'eth': 1.0, 'usdt': 2.0, 'usdc': 3.0, 'usd': 9.0, 'blk': 1, 't': '2026-09-26T07:00:00+00:00', 'n': 9}},
                'transfery': [], 'ostatni_blok': 5000, 'okno': 10, 'eth_usd': 2500.0, 'eth_usd_at': '2026-09-26T07:00:00+00:00'}
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd.time, 'sleep'), mock.patch.object(zd.time, 'monotonic', lambda: clock[0]):
            with self.assertRaises(RuntimeError) as cm:
                zd.build_wieloryby(prev)
        self.assertEqual(len(calls), 2, 'dwie próby paczki sald (20 s + 20 s), potem koniec — bez zapasu i bez ponownego pytania o głowicę')
        self.assertLessEqual(clock[0] - 1000.0, zd.WH_LIMIT, 'cały przebieg w budżecie 40 s'); self.assertTrue(all(tm <= 20 for _, _, tm in calls))
        self.assertIn('budżet czasu', str(cm.exception)); self.assertIn('timed out', str(cm.exception))
        # wolny, ale odpowiadający węzeł: drugie żądanie paczki dostaje tylko resztę budżetu; po terminie — bez żadnego żądania
        clock[0], seen = 1000.0, []

        def slow(url, body, timeout=60):
            seen.append(timeout); clock[0] += 15; return [{'jsonrpc': '2.0', 'id': b['id'], 'result': '0x1'} for b in body]
        with mock.patch.object(zd, 'post_json', side_effect=slow), mock.patch.object(zd.time, 'monotonic', lambda: clock[0]):
            r = zd.wh_rpc([('eth_getBalance', ['0x1', 'latest'])] * 50, termin=1025.0)
            self.assertEqual(r, ['0x1'] * 50); self.assertEqual(seen, [20, 10], 'limit czasu żądania = min(20 s, reszta budżetu)')
            with self.assertRaises(RuntimeError) as cm:
                zd.wh_rpc([('eth_blockNumber', [])], termin=1025.0)
        self.assertEqual(len(seen), 2, 'po terminie żadne żądanie nie wychodzi'); self.assertIn('budżet czasu', str(cm.exception))

    def test_logs_result_not_list_is_a_failure_not_empty(self):
        """„result: null” dla eth_getLogs to brak odpowiedzi, nie „brak zdarzeń”: paczka nieudana, ostatni_blok nie idzie dalej."""
        post, _ = self._rpc()
        with mock.patch.object(zd, 'post_json', side_effect=post):
            prev = zd.build_wieloryby(None)
        prev['at'] = '2026-09-26T07:00:00+00:00'; prev['part_at'] = {'salda': '2026-09-26T07:00:00+00:00', 'transfery': '2026-09-26T06:40:00+00:00'}
        base, _ = self._rpc(head=10100)

        def nul(url, body, timeout=60):
            return [dict(x, result=None) if b['method'] == 'eth_getLogs' else x for x, b in zip(base(url, body, timeout), body)]
        with mock.patch.object(zd, 'post_json', side_effect=nul), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_wieloryby(prev)
        self.assertEqual(o['ok'], {'cena': True, 'salda': True, 'transfery': False})
        self.assertEqual(o['ostatni_blok'], 10000, 'blok „zeskanowany” nie przesuwa się po odpowiedzi null'); self.assertIs(o['transfery'], prev['transfery'])
        self.assertEqual(o['part_at']['transfery'], '2026-09-26T06:40:00+00:00'); self.assertEqual(len(zd.META['errors']), 1)
        self.assertIn('nie jest listą', zd.META['errors'][0]); self.assertTrue(zd.META['errors'][0].startswith('Wieloryby: transfery: logi bloków 10001–10100'))
        # null dopiero w drugiej paczce: postęp do końca pierwszej zostaje, część oznaczona jako nieudana z błędem
        zd.META['errors'].clear()
        base2, _ = self._rpc(head=11700)

        def nul2(url, body, timeout=60):
            return [dict(x, result=None) if b['method'] == 'eth_getLogs' and int(b['params'][0]['fromBlock'], 16) >= 10801 else x for x, b in zip(base2(url, body, timeout), body)]
        with mock.patch.object(zd, 'post_json', side_effect=nul2), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_wieloryby(prev)
        self.assertEqual((o['ok']['transfery'], o['ostatni_blok']), (False, 10800), 'zeskanowano tylko pierwszą paczkę')
        self.assertEqual(o['part_at']['transfery'], zd.NOW); self.assertIn('zeskanowano do bloku 10800', zd.META['errors'][0]); self.assertIn('10801–11600', zd.META['errors'][0])

    def test_pary_wewnetrzne(self):
        R = lambda tx, d, amt, blk, exch='OKX', tok='USDC': {'tx': tx, 'li': 1, 'exch': exch, 'dir': d, 'token': tok, 'amt': amt, 'blk': blk}
        rows = [R('0x1', 'out', 37826063.2, 26058886), R('0x2', 'in', 37826063.2, 26058900),   # ta sama kwota, 14 bloków = para
                R('0x3', 'in', 37826063.2, 26059000),                                            # 114 bloków — za daleko
                R('0x4', 'in', 37826063.2, 26058890, exch='Binance'),                            # inna giełda
                R('0x5', 'in', 37826063.2, 26058890, tok='USDT'),                                # inny token
                R('0x6', 'in', 37826063.21, 26058890), 'śmieć', {'dir': 'in', 'blk': 'x'}]       # inna kwota, śmieci
        self.assertIs(zd.wh_pary(rows), rows)
        self.assertEqual([r.get('wew') for r in rows if isinstance(r, dict) and 'tx' in r], [True, True, None, None, None, None])
        self.assertEqual(zd.wh_pary([]), [])
        # raz nadane oznaczenie zostaje, gdy druga strona pary wypadnie z okna (wiersze idą z poprzedniego pliku)
        solo = [rows[1]]
        self.assertTrue(zd.wh_pary(solo)[0]['wew'])

    def test_main_uses_previous_file_on_failure(self):
        saved = {}
        prev = {'at': '2026-09-26T07:00:00+00:00', 'salda': {'Binance': {'eth': 1.0}}}
        with mock.patch.object(zd, 'build_wieloryby', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)):
            src = open(zd.__file__, encoding='utf-8').read()
            self.assertIn("prev_wh = previous('wieloryby')", src); self.assertIn("META['errors'].append(mask(f'Wieloryby: {e}')); META['ok']['wieloryby'] = False", src)
            self.assertIn("if prev_wh: save('wieloryby', prev_wh)", src); self.assertIn("'build_rynki', 'build_wieloryby'", open(__file__, encoding='utf-8').read())

import re as _re_v106


class IndeksyV106(unittest.TestCase):
    """v106: indeksy świata (EODHD, rotacja 20 zapytań na dobę) i notowania ETF (Massive, zapas Tiingo): brak ≠ zero,
    część z błędem = poprzednia wersja z własnym czasem, dobowy limit pilnowany, klucze maskowane, brak klucza = informacja."""
    UTC = datetime.timezone.utc
    FRI = datetime.datetime(2026, 9, 25, 18, 30, tzinfo=datetime.timezone.utc)   # piątek 18:30 UTC: Europa i Azja po sesji, Ameryki jeszcze nie
    EOD = [{'date': '2026-09-22', 'open': 1, 'close': 6650.5, 'volume': 1}, {'date': '2026-09-23', 'close': None}, {'date': '2026-09-24', 'close': '6702.1'},
           {'date': 'x', 'close': 1}, {'date': '2026-09-25', 'close': 0}, 'śmieć']
    MAS = {'status': 'OK', 'resultsCount': 4, 'results': [{'T': 'SPY', 'c': 690.12}, {'T': 'EWJ', 'c': None}, {'T': 'ZZZ', 'c': 1}, {'T': 'GLD', 'c': '410.5'}]}
    TII = [{'date': '2026-09-23T00:00:00.000Z', 'close': 688.0, 'adjClose': 688.0}, {'date': '2026-09-24T00:00:00.000Z', 'close': None},
           {'date': '2026-09-25T00:00:00.000Z', 'close': 690.5}]
    STUBS = tuple(n for n in dir(zd) if n.startswith('build_') and n != 'build_indeksy')   # wszystkie pozostałe źródła udają awarię (bez sieci)

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    @staticmethod
    def _http(code):
        return zd.urllib.error.HTTPError('u', code, 'x', {}, None)

    def _gj(self, calls, eod=None, massive=None, tiingo=None):
        def gj(url, headers=None, timeout=30):
            calls.append(url)
            for host, r, tmax in (('eodhd.com', eod, zd.IX_TIMEOUT), ('api.massive.com', massive, zd.ETF_TIMEOUT), ('api.tiingo.com', tiingo, zd.TIINGO_TIMEOUT)):
                if host in url:
                    self.assertLessEqual(timeout, min(tmax, 20), 'limit czasu zapytania: ' + host)
                    r = r(url) if callable(r) else r
                    if isinstance(r, Exception):
                        raise r
                    return r
            raise AssertionError('nieznany adres ' + url)
        return gj

    def test_parsers_missing_is_not_zero(self):
        self.assertEqual(zd.eod_parse(self.EOD), [['2026-09-22', 6650.5], ['2026-09-24', 6702.1]])   # None, 0, zła data, śmieć — brak, nie zero
        with self.assertRaises(RuntimeError):
            zd.eod_parse({'message': 'Forbidden'})
        with self.assertRaises(ValueError):
            zd.eod_parse([{'date': '2026-09-24', 'close': None}])
        self.assertEqual(zd.massive_parse(self.MAS, zd.IX_ETF), {'SPY': 690.12, 'GLD': 410.5})
        self.assertEqual(zd.massive_parse({'status': 'DELAYED', 'results': [{'T': 'SPY', 'c': 1.5}]}, ('SPY',)), {'SPY': 1.5})
        with self.assertRaises(RuntimeError):
            zd.massive_parse({'status': 'ERROR', 'error': 'plan'}, zd.IX_ETF)
        self.assertEqual(zd.tiingo_parse(self.TII), [['2026-09-23', 688.0], ['2026-09-25', 690.5]])
        with self.assertRaises(RuntimeError):
            zd.tiingo_parse({'detail': 'Invalid token'})

    def test_merge_keeps_last_and_drops_broken(self):
        self.assertEqual(zd._ix_merge([['2026-09-22', 1.0], ['2026-09-24', 2.0], ['x'], ['2026-09-23', None]], [['2026-09-24', 2.5], ['2026-09-25', 3.0]], 3),
                         [['2026-09-22', 1.0], ['2026-09-24', 2.5], ['2026-09-25', 3.0]])
        self.assertEqual(zd._ix_merge(None, [['2026-09-25', 3.0]], 265), [['2026-09-25', 3.0]])

    def test_ready_and_plan(self):
        r = zd.ix_ready
        self.assertEqual(r(22, self.FRI), datetime.datetime(2026, 9, 24, 22, tzinfo=self.UTC))   # USA: piątkowa sesja jeszcze trwa → czwartek
        self.assertEqual(r(17, self.FRI), datetime.datetime(2026, 9, 25, 17, tzinfo=self.UTC))
        self.assertEqual(r(7, datetime.datetime(2026, 9, 27, 12, tzinfo=self.UTC)), datetime.datetime(2026, 9, 25, 7, tzinfo=self.UTC))   # niedziela → piątek
        part = {s: {'at': '2026-09-25T17:30:00+00:00', 'd': [['2026-09-25', 1.0]]} for s, _, _ in zd.IX_SYMBOLS}
        part['GSPC']['at'] = '2026-09-24T23:00:00+00:00'   # pobrane po czwartkowej sesji USA — piątkowa jeszcze trwa, nie do odświeżenia
        part['N225']['at'] = '2026-09-24T08:00:00+00:00'   # sprzed piątkowej sesji w Tokio — do odświeżenia
        part['XU100']['at'] = '2026-09-23T08:00:00+00:00'  # jeszcze starsze — pierwsze z pobranych
        del part['JKSE']                                    # nigdy nie pobrane — na początku kolejki
        part['HSI'] = {'at': '2026-09-20T08:00:00+00:00', 'bad_at': '2026-09-24T09:00:00+00:00', 'bad_n': 3, 'bad': 404}   # odrzucony 3 razy — 4 dni przerwy
        part['SSEC'] = {'at': '2026-09-24T10:00:00+00:00', 'd': [['2026-09-24', 1.0]], 'bad_at': '2026-09-24T09:00:00+00:00', 'bad_n': 1, 'bad': 403}   # raz — doba minęła, znów w kolejce
        self.assertEqual(zd.ix_plan(part, self.FRI, 20), ['JKSE', 'XU100', 'N225', 'SSEC'])
        self.assertEqual(zd.ix_plan(part, self.FRI, 1), ['JKSE'])
        self.assertEqual(zd.ix_plan(part, self.FRI, 0), [])
        self.assertEqual(zd.ix_plan({}, self.FRI, 20), ['GSPC', 'IXIC', 'DJI', 'GSPTSE'])   # pusty plik: kolejność listy, najwyżej IX_PER_RUN
        self.assertEqual(zd.IX_PER_RUN, 4); self.assertEqual(zd.IX_DAILY, 20); self.assertEqual(len(zd.IX_SYMBOLS), 23)   # v118.2: bez FTSE i FTMIB (poza planem)
        self.assertEqual([zd._ix_pause({'bad_n': n}) for n in (1, 2, 3, 4, 9)], [1, 2, 4, 7, 7], 'przerwa rośnie: 1, 2, 4, potem tydzień')
        self.assertEqual((zd._ix_pause({}), zd._ix_pause({'bad_n': 'x'}), zd._ix_pause({'bad_n': 0})), (1, 1, 1))
        p = {'GSPC': {'cc': 'us', 'at': 'x', 'd': [['2026-09-24', 1.0]], 'bad_n': 1}}
        zd._ix_bad(p, 'GSPC', 403); zd._ix_bad(p, 'ZZZ', 404)
        self.assertEqual(p['GSPC'], {'cc': 'us', 'at': 'x', 'd': [['2026-09-24', 1.0]], 'bad_at': zd.NOW, 'bad_n': 2, 'bad': 403}, 'stara seria zostaje, licznik rośnie')
        self.assertEqual(p['ZZZ'], {'bad_at': zd.NOW, 'bad_n': 1, 'bad': 404})

    def test_build_first_run_rotation_merge_and_cached_etf(self):
        calls = []
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=list(self.EOD), massive=self.MAS)):
            o = zd.build_indeksy({'EODHD_KEY': 'k-eod', 'MASSIVE_KEY': 'k-mas'}, None, now=self.FRI)
        self.assertEqual(o['ok'], {'ix': True, 'etf': True})
        self.assertEqual(sorted(o['ix']), ['DJI', 'GSPC', 'GSPTSE', 'IXIC'], 'pierwszy przebieg: IX_PER_RUN indeksów')
        self.assertEqual(o['ix']['GSPC'], {'cc': 'us', 'at': zd.NOW, 'd': [['2026-09-22', 6650.5], ['2026-09-24', 6702.1]]})
        self.assertEqual(o['ix_calls'], {'d': '2026-09-25', 'n': 4}); self.assertNotIn('ix_quota', o)
        self.assertEqual(o['part_at'], {'ix': zd.NOW, 'etf': zd.NOW})
        self.assertEqual(o['etf'], {'date': '2026-09-24', 'src': 'massive', 'q': {'SPY': [['2026-09-24', 690.12]], 'GLD': [['2026-09-24', 410.5]]}})
        eod_urls = [c for c in calls if 'eodhd' in c]
        self.assertEqual(len(eod_urls), 4)
        self.assertEqual(eod_urls[0], 'https://eodhd.com/api/eod/GSPC.INDX?api_token=k-eod&fmt=json&period=d&from=2025-09-20', 'pierwsze pobranie: rok wstecz')
        self.assertEqual([c for c in calls if 'massive' in c], ['https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-09-24?adjusted=true&apiKey=k-mas'])
        self.assertEqual(zd.META['errors'], [])
        # godzinę później: kolejne 4 z listy, USA nie ponownie; notowania ETF młodsze niż 6 h — bez zapytania
        prev, calls = o, []
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=[{'date': '2026-09-25', 'close': 1.0}], massive=AssertionError('nie pytać'))):
            o2 = zd.build_indeksy({'EODHD_KEY': 'k-eod', 'MASSIVE_KEY': 'k-mas'}, prev, now=self.FRI + datetime.timedelta(hours=1))
        self.assertEqual(sorted(o2['ix']), ['BVSP', 'DJI', 'FCHI', 'GDAXI', 'GSPC', 'GSPTSE', 'IXIC', 'MXX'])
        self.assertEqual(o2['ix_calls'], {'d': '2026-09-25', 'n': 8}, 'dobowy licznik rośnie')
        self.assertEqual(o2['ix']['GSPC'], prev['ix']['GSPC']); self.assertIs(o2['etf'], prev['etf']); self.assertEqual(o2['part_at']['etf'], zd.NOW)
        # sobota 1:00 UTC, USA pobrane przed piątkową sesją: dopełnienie z zakładką 10 dni, nowsza wartość wygrywa, ponad rok przycięty, licznik od zera
        for s, c, _ in zd.IX_SYMBOLS:
            o2['ix'].setdefault(s, {'cc': c, 'at': '2026-09-26T00:30:00+00:00', 'd': [['2026-09-25', 1.0]]})   # reszta świeżo po sesji — nie do odświeżenia
        o2['ix']['GSPC']['at'] = '2026-09-24T23:00:00+00:00'; o2['ix']['GSPC']['d'] = [['2025-%02d-%02d' % (1 + i // 28, 1 + i % 28), 1.0] for i in range(263)] + o2['ix']['GSPC']['d']
        calls = []
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=[{'date': '2026-09-24', 'close': 6700.0}, {'date': '2026-09-25', 'close': 6750.0}], massive=self.MAS)):
            o3 = zd.build_indeksy({'EODHD_KEY': 'k-eod', 'MASSIVE_KEY': 'k-mas'}, o2, now=datetime.datetime(2026, 9, 26, 1, 0, tzinfo=self.UTC))
        self.assertEqual(calls, ['https://eodhd.com/api/eod/GSPC.INDX?api_token=k-eod&fmt=json&period=d&from=2026-09-14'], 'tylko USA, od ostatniej sesji minus 10 dni')
        g = o3['ix']['GSPC']['d']
        self.assertEqual(g[-3:], [['2026-09-22', 6650.5], ['2026-09-24', 6700.0], ['2026-09-25', 6750.0]]); self.assertEqual(len(g), zd.IX_KEEP)
        self.assertEqual(o3['ix_calls'], {'d': '2026-09-26', 'n': 1}); self.assertEqual(o3['ok'], {'ix': True, 'etf': True})

    def test_402_keeps_previous_series_and_blocks_the_day(self):
        prev = {'at': '2026-09-25T10:00:00+00:00', 'ok': {'ix': True}, 'part_at': {'ix': '2026-09-25T10:00:00+00:00'}, 'ix_calls': {'d': '2026-09-25', 'n': 18},
                'ix': {'GSPC': {'cc': 'us', 'at': '2026-09-24T23:00:00+00:00', 'd': [['2026-09-24', 6702.1]]},
                       'GDAXI': {'cc': 'de', 'at': '2026-09-24T18:00:00+00:00', 'd': [['2026-09-24', 24100.0]]}}}
        calls = []
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=self._http(402))):
            o = zd.build_indeksy({'EODHD_KEY': 'k'}, prev, now=self.FRI)
        self.assertEqual(o['ok'], {'ix': False}); self.assertEqual(o['ix_quota'], '2026-09-25')
        self.assertEqual(len(calls), 1, 'po 402 koniec zapytań w tym przebiegu')
        self.assertEqual(o['ix']['GDAXI']['d'], [['2026-09-24', 24100.0]], 'poprzednia seria zostaje (z własną datą), nie zero')
        self.assertEqual(o['ix']['GSPC'], prev['ix']['GSPC']); self.assertEqual(o['ix_calls'], {'d': '2026-09-25', 'n': 19})
        self.assertEqual(o['part_at']['ix'], '2026-09-25T10:00:00+00:00', 'część z błędem — poprzedni czas')
        self.assertTrue(any(e.startswith('Indeksy: EODHD HTTP 402') for e in zd.META['errors']), zd.META['errors'])
        calls.clear(); zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=self._http(402))):
            o2 = zd.build_indeksy({'EODHD_KEY': 'k'}, o, now=self.FRI + datetime.timedelta(hours=1))
        self.assertEqual(calls, [], 'blokada do końca dnia — zero zapytań'); self.assertEqual(o2['ok'], {'ix': True}); self.assertEqual(o2['ix_quota'], '2026-09-25')
        self.assertEqual(zd.META['errors'], []); self.assertTrue(any(n.startswith('Indeksy: limit dobowy') for n in zd.META['notes']))
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=[{'date': '2026-09-25', 'close': 2.0}])):
            o3 = zd.build_indeksy({'EODHD_KEY': 'k'}, o2, now=self.FRI + datetime.timedelta(hours=6))   # nowy dzień UTC — blokada znika
        self.assertEqual(len(calls), zd.IX_PER_RUN); self.assertNotIn('ix_quota', o3); self.assertEqual(o3['ok'], {'ix': True})

    @staticmethod
    def _syms(calls):
        return [c.split('/eod/')[1].split('.')[0] for c in calls if 'eodhd' in c]

    def test_403_first_run_minimal_file_then_rotation_and_recovery(self):
        # pierwszy przebieg, każda próba odrzucona (zły klucz): plik minimalny z licznikiem dobowym i znacznikami — nie wyjątek,
        # więc main go zapisuje, fresh() działa i zbieracz wraca za godzinę (nie co 20 min), a jeden komunikat w META (nie dwa)
        calls, h = [], datetime.timedelta(hours=1)
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=self._http(403))), mock.patch.object(zd, 'NOW', self.FRI.isoformat()):
            o = zd.build_indeksy({'EODHD_KEY': 'k'}, None, now=self.FRI)
        self.assertEqual(self._syms(calls), ['GSPC', 'IXIC', 'DJI', 'GSPTSE'], 'odrzucony kod nie zatrzymuje pozostałych prób')
        self.assertEqual(o['ok'], {'ix': False}); self.assertEqual(o['ix_calls'], {'d': '2026-09-25', 'n': 4}); self.assertEqual(o['part_at'], {}); self.assertNotIn('ix_quota', o)
        self.assertEqual(o['ix'], {s: {'bad_at': self.FRI.isoformat(), 'bad_n': 1, 'bad': 403} for s in ('GSPC', 'IXIC', 'DJI', 'GSPTSE')}, 'każda próba odrzucona: doba przerwy, nie dłużej')
        self.assertEqual(len(zd.META['errors']), 1); e = zd.META['errors'][0]
        self.assertTrue(e.startswith('Indeksy: ') and 'klucz odrzucony' in e and 'HTTP 403 — odrzucony kod GSPC.INDX' in e, e)
        # godzinę później: pierwsze cztery mają przerwę — kolej na następne cztery (nie w kółko te same)
        calls.clear(); zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=self._http(403))), mock.patch.object(zd, 'NOW', (self.FRI + h).isoformat()):
            o2 = zd.build_indeksy({'EODHD_KEY': 'k'}, o, now=self.FRI + h)
        self.assertEqual(self._syms(calls), ['BVSP', 'MXX', 'GDAXI', 'FCHI']); self.assertEqual(o2['ix_calls']['n'], 8); self.assertEqual(len(o2['ix']), 8)
        # dobę później klucz poprawiony: najdłużej czekające wracają od razu (przerwa po „klucz odrzucony” = doba)
        calls.clear(); zd.META['errors'].clear(); d1 = self.FRI + datetime.timedelta(days=1, hours=1)
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=[{'date': '2026-09-25', 'close': 5.0}])), mock.patch.object(zd, 'NOW', d1.isoformat()):
            o3 = zd.build_indeksy({'EODHD_KEY': 'k'}, o2, now=d1)
        self.assertEqual(self._syms(calls), ['GSPC', 'IXIC', 'DJI', 'GSPTSE']); self.assertEqual(o3['ok'], {'ix': True}); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(o3['ix']['GSPC'], {'cc': 'us', 'at': d1.isoformat(), 'd': [['2026-09-25', 5.0]]}, 'udane pobranie kasuje znacznik'); self.assertEqual(o3['part_at']['ix'], d1.isoformat())
        self.assertEqual(o3['ix_calls'], {'d': '2026-09-26', 'n': 4})

    def test_single_403_does_not_stall_the_rotation(self):
        # jeden kod, którego plan nie obejmuje (AEX → 403), wśród działających: dostaje rosnącą przerwę, reszta rotacji idzie dalej
        eod = lambda url: self._http(403) if 'AEX' in url else [{'date': '2026-09-25', 'close': 5.0}]
        prev = {'at': '2026-09-25T17:40:00+00:00', 'ok': {'ix': True}, 'part_at': {'ix': '2026-09-25T17:40:00+00:00'}, 'ix_calls': {'d': '2026-09-25', 'n': 3},
                'ix': {s: {'cc': c, 'at': '2026-09-25T17:30:00+00:00', 'd': [['2026-09-25', 1.0]]} for s, c, _ in zd.IX_SYMBOLS if s not in ('AEX', 'SSMI', 'OMXS30')}}
        runs = []
        for k, (dt, due) in enumerate([(self.FRI, ['AEX', 'SSMI', 'OMXS30']),                                  # A: nigdy nie pobrane, kolejność listy
                                       (self.FRI + datetime.timedelta(hours=1), []),                            # B: AEX ma dobę przerwy, reszta świeża
                                       (self.FRI + datetime.timedelta(days=1, hours=1), ['AEX', 'WIG20']),    # C: doba minęła — druga próba obok działającego
                                       (self.FRI + datetime.timedelta(days=2, hours=1), []),                    # D: 2 dni przerwy
                                       (self.FRI + datetime.timedelta(days=3, hours=2), ['AEX', 'WIG20'])]):  # E: trzecia próba → 4 dni
            calls = []; zd.META['errors'].clear()
            for r in prev['ix'].values():
                if r.get('d'):
                    r['at'] = dt.isoformat()   # reszta „świeżo po sesji” — w tym teście liczy się tylko AEX (i WIG20 w C, E)
            if k in (2, 4):
                prev['ix']['WIG20']['at'] = '2026-09-25T10:00:00+00:00'   # sprzed sesji — do odświeżenia razem z AEX
            with mock.patch.object(zd, 'get_json', self._gj(calls, eod=eod)), mock.patch.object(zd, 'NOW', dt.isoformat()):
                prev = zd.build_indeksy({'EODHD_KEY': 'k'}, prev, now=dt)
            self.assertEqual(self._syms(calls), due, f'przebieg {k}')
            runs.append(prev)
        self.assertEqual(runs[0]['ok'], {'ix': False}); self.assertEqual(runs[0]['part_at']['ix'], self.FRI.isoformat(), 'coś odświeżono — czas części bieżący')
        self.assertEqual(runs[0]['ix']['SSMI']['d'], [['2026-09-25', 5.0]]); self.assertEqual(runs[0]['ix']['AEX'], {'bad_at': self.FRI.isoformat(), 'bad_n': 1, 'bad': 403})
        self.assertEqual(runs[1]['ok'], {'ix': True}, 'nic do zrobienia = bez błędu')
        self.assertEqual(runs[2]['ix']['AEX']['bad_n'], 2); self.assertEqual(runs[4]['ix']['AEX']['bad_n'], 3); self.assertEqual(zd._ix_pause(runs[4]['ix']['AEX']), 4)
        self.assertEqual(len(runs[4]['ix']), 23); self.assertEqual(sum(1 for r in runs[4]['ix'].values() if r.get('d')), 22)   # v118.2: 23 kody
        e = ' '.join(zd.META['errors'])
        self.assertIn('HTTP 403 — odrzucony kod AEX.INDX', e); self.assertNotIn('klucz odrzucony', e, 'inne próby udane — to kod, nie klucz')

    def test_404_and_other_errors(self):
        calls = []

        def eod(url):
            if 'IXIC' in url:
                return self._http(404)
            if 'DJI' in url:
                return self._http(500)
            return [{'date': '2026-09-25', 'close': 5.0}]
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=eod)):
            o = zd.build_indeksy({'EODHD_KEY': 'k'}, None, now=self.FRI)
        self.assertEqual(o['ok'], {'ix': False}); self.assertEqual(sorted(o['ix']), ['GSPC', 'GSPTSE', 'IXIC'])
        self.assertEqual(o['ix']['IXIC'], {'bad_at': zd.NOW, 'bad_n': 1, 'bad': 404}); self.assertNotIn('DJI', o['ix'], 'HTTP 500 — bez wpisu, nie zero')
        self.assertEqual(o['ix']['GSPC']['d'], [['2026-09-25', 5.0]]); self.assertEqual(o['part_at']['ix'], zd.NOW, 'coś odświeżono — czas części bieżący')
        self.assertEqual(len(calls), 4)
        e = ' '.join(zd.META['errors'])
        self.assertIn('nieznany kod IXIC.INDX', e); self.assertIn('HTTP 500 (DJI)', e); self.assertNotIn('klucz odrzucony', e)
        calls.clear()
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=eod)):
            zd.build_indeksy({'EODHD_KEY': 'k'}, o, now=self.FRI + datetime.timedelta(hours=1))
        self.assertFalse(any('IXIC' in c for c in calls), 'kod z 404 — przerwa'); self.assertIn('DJI', self._syms(calls), 'po HTTP 500 następna próba za godzinę')

    def test_no_response_and_time_budget_keep_the_run_short(self):
        # Massive 429, Tiingo bez odpowiedzi (przekroczony czas): po dwóch brakach z rzędu koniec — nie 40 × limit czasu; poprzednia część zostaje
        calls = []
        prev = {'at': '2026-09-25T09:00:00+00:00', 'part_at': {'etf': '2026-09-25T09:00:00+00:00'}, 'ok': {'etf': True}, 'etf': {'date': '2026-09-24', 'src': 'massive', 'q': {'SPY': [['2026-09-24', 690.0]]}}}
        with mock.patch.object(zd, 'get_json', self._gj(calls, massive=self._http(429), tiingo=TimeoutError('timed out'))), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_indeksy({'MASSIVE_KEY': 'm', 'TIINGO_KEY': 't'}, prev, now=self.FRI)
        self.assertEqual(len([c for c in calls if 'tiingo' in c]), zd.IX_MISS_MAX); self.assertEqual(zd.IX_MISS_MAX, 2)
        self.assertEqual(o['ok'], {'etf': False}); self.assertIs(o['etf'], prev['etf']); self.assertEqual(o['part_at']['etf'], '2026-09-25T09:00:00+00:00')
        self.assertIn('brak odpowiedzi', ' '.join(zd.META['errors']))
        # EODHD bez odpowiedzi: dwa braki z rzędu kończą pętlę; plik minimalny z licznikiem (dwa zapytania policzone)
        calls.clear(); zd.META['errors'].clear()
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=ConnectionResetError('reset'))):
            o2 = zd.build_indeksy({'EODHD_KEY': 'k'}, None, now=self.FRI)
        self.assertEqual(len(calls), 2); self.assertEqual(o2['ok'], {'ix': False}); self.assertEqual(o2['ix_calls'], {'d': '2026-09-25', 'n': 2}); self.assertNotIn('ix', o2)
        # udana odpowiedź między brakami zeruje licznik braków
        calls.clear(); zd.META['errors'].clear()
        seq = iter([ConnectionResetError('a'), [{'date': '2026-09-25', 'close': 1.0}], ConnectionResetError('b'), [{'date': '2026-09-25', 'close': 2.0}]])
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=lambda url: next(seq))):
            o3 = zd.build_indeksy({'EODHD_KEY': 'k'}, None, now=self.FRI)
        self.assertEqual(len(calls), 4); self.assertEqual(sorted(o3['ix']), ['GSPTSE', 'IXIC'])
        # budżet czasu całego budowniczego: zegar skacze o 30 s na odczyt — po pierwszym zapytaniu koniec, część etf nawet nie zaczyna
        calls.clear(); zd.META['errors'].clear(); clock = [0.0]

        def mono():
            clock[0] += 30.0
            return clock[0]
        with mock.patch.object(zd, 'get_json', self._gj(calls, eod=[{'date': '2026-09-25', 'close': 1.0}], massive=self.MAS)), mock.patch.object(zd.time, 'monotonic', mono):
            o4 = zd.build_indeksy({'EODHD_KEY': 'k', 'MASSIVE_KEY': 'm'}, None, now=self.FRI)
        self.assertEqual(self._syms(calls), ['GSPC']); self.assertEqual([c for c in calls if 'massive' in c], [])
        self.assertEqual(o4['ok'], {'ix': False, 'etf': False}); self.assertEqual(sorted(o4['ix']), ['GSPC']); self.assertEqual(o4['part_at'], {'ix': zd.NOW})
        self.assertIn('budżet czasu', ' '.join(zd.META['errors']))
        self.assertLess(zd.IX_BUDGET_S + max(zd.IX_TIMEOUT, zd.ETF_TIMEOUT, zd.TIINGO_TIMEOUT), 60, 'budowniczy zawsze poniżej minuty (budżet + jedno zapytanie w toku)')
        self.assertTrue(max(zd.IX_TIMEOUT, zd.ETF_TIMEOUT, zd.TIINGO_TIMEOUT) <= 20)

    def test_etf_massive_fails_then_tiingo_then_previous(self):
        calls = []
        zd.SECRETS[:] = ['SEKRET-MASSIVE', 'SEKRET-TIINGO']
        try:
            with mock.patch.object(zd, 'get_json', self._gj(calls, massive=self._http(429), tiingo=self.TII)), mock.patch.object(zd.time, 'sleep'):
                o = zd.build_indeksy({'MASSIVE_KEY': 'SEKRET-MASSIVE', 'TIINGO_KEY': 'SEKRET-TIINGO'}, None, now=self.FRI)
            self.assertEqual(o['ok'], {'etf': True}); self.assertNotIn('ix', o)
            self.assertEqual((o['etf']['src'], o['etf']['date']), ('tiingo', '2026-09-25'))
            self.assertEqual(o['etf']['q']['SPY'], [['2026-09-23', 688.0], ['2026-09-25', 690.5]])
            tu = [c for c in calls if 'tiingo' in c]
            self.assertEqual(len(tu), min(zd.ETF_TIINGO_MAX, len(zd.IX_ETF)))
            self.assertEqual(tu[0], 'https://api.tiingo.com/tiingo/daily/SPY/prices?token=SEKRET-TIINGO&startDate=2026-08-11')
            self.assertTrue(any('Massive HTTP 429' in e for e in zd.META['errors']), 'awaria Massive widoczna mimo zapasu')
            self.assertNotIn('SEKRET', ' '.join(zd.META['errors'] + zd.META['notes']))
            prev = {'at': '2026-09-25T09:00:00+00:00', 'part_at': {'etf': '2026-09-25T09:00:00+00:00'}, 'ok': {'etf': True}, 'etf': o['etf']}
            zd.META['errors'].clear(); calls.clear()
            with mock.patch.object(zd, 'get_json', self._gj(calls, massive=self._http(429), tiingo=self._http(403))), mock.patch.object(zd.time, 'sleep'):
                o2 = zd.build_indeksy({'MASSIVE_KEY': 'SEKRET-MASSIVE', 'TIINGO_KEY': 'SEKRET-TIINGO'}, prev, now=self.FRI)
            self.assertEqual(o2['ok'], {'etf': False}); self.assertIs(o2['etf'], prev['etf']); self.assertEqual(o2['part_at']['etf'], '2026-09-25T09:00:00+00:00')
            self.assertEqual(len([c for c in calls if 'tiingo' in c]), 1, 'po 403 koniec pytania Tiingo')
            self.assertTrue(any(e.startswith('Indeksy: ') and 'ETF' in e for e in zd.META['errors']))
            # święto w USA: dzień bez notowań → o jeden dzień roboczy wstecz
            calls.clear(); zd.META['errors'].clear()
            mas = lambda url: {'status': 'OK', 'resultsCount': 0, 'results': []} if '2026-09-24' in url else self.MAS
            with mock.patch.object(zd, 'get_json', self._gj(calls, massive=mas)):
                o3 = zd.build_indeksy({'MASSIVE_KEY': 'SEKRET-MASSIVE'}, None, now=self.FRI)
            self.assertEqual((o3['etf']['date'], len(calls)), ('2026-09-23', 2)); self.assertEqual(zd.META['errors'], [])
            with self.assertRaises(RuntimeError):
                zd.build_indeksy({}, None, now=self.FRI)   # bez kluczy i bez poprzedniego pliku — nic do zapisania
        finally:
            zd.SECRETS[:] = []

    def test_main_flow_keys_notes_and_cache(self):
        stubs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in self.STUBS]
        saved, prev, calls = {}, {}, []
        env = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY',
                               'MASSIVE_KEY', 'TIINGO_KEY', 'FMP_KEY', 'ALPHAVANTAGE_KEY')}
        env['EODHD_KEY'] = 'SEKRET-EODHD'
        built = {'at': zd.NOW, 'ok': {'ix': True}, 'part_at': {'ix': zd.NOW}, 'ix': {'GSPC': {'cc': 'us', 'at': zd.NOW, 'd': [['2026-09-25', 1.0]]}}}
        for s in stubs:
            s.start()
        try:
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                    mock.patch.object(zd, 'previous', lambda n: prev.get(n)), \
                    mock.patch.object(zd, 'build_indeksy', lambda k, p, now=None: calls.append((dict(k), p)) or built):
                zd.main()
            self.assertEqual(calls, [({'EODHD_KEY': 'SEKRET-EODHD', 'MASSIVE_KEY': '', 'TIINGO_KEY': '', 'FMP_KEY': '', 'ALPHAVANTAGE_KEY': ''}, None)])
            self.assertIn('SEKRET-EODHD', zd.SECRETS, 'klucz maskowany w komunikatach'); self.assertIs(saved['indeksy'], built)
            self.assertIs(zd.META['ok']['indeksy_ix'], True); self.assertNotIn('indeksy_etf', zd.META['ok'])
            self.assertTrue(any(n.startswith('brak MASSIVE_KEY') for n in zd.META['notes']) and any(n.startswith('brak TIINGO_KEY') for n in zd.META['notes']))
            self.assertFalse(any('EODHD' in e or 'MASSIVE' in e or 'TIINGO' in e for e in zd.META['errors']), 'brak klucza to informacja, nie błąd')
            self.assertNotIn('SEKRET-EODHD', json.dumps(saved['meta']))
            prev['indeksy'] = {'at': _iso(10), 'ok': {'ix': True, 'etf': False}, 'ix': {}}
            calls.clear(); zd.META['ok'].clear(); zd.META['errors'].clear()
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                    mock.patch.object(zd, 'previous', lambda n: prev.get(n)), \
                    mock.patch.object(zd, 'build_indeksy', side_effect=AssertionError('plik młodszy niż godzina — nie budować')):
                zd.main()
            self.assertIs(saved['indeksy'], prev['indeksy']); self.assertEqual(zd.META['ok']['indeksy_ix'], 'cached'); self.assertIs(zd.META['ok']['indeksy_etf'], False)
            prev['indeksy']['at'] = _iso(120); zd.META['ok'].clear(); zd.META['errors'].clear()
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                    mock.patch.object(zd, 'previous', lambda n: prev.get(n)), \
                    mock.patch.object(zd, 'build_indeksy', side_effect=RuntimeError('offline')):
                zd.main()
            self.assertIs(saved['indeksy'], prev['indeksy'], 'awaria — zostaje poprzedni plik'); self.assertIs(zd.META['ok']['indeksy_ix'], False)
            self.assertIn('Indeksy: offline', zd.META['errors'])
            env['EODHD_KEY'] = ''; saved.clear(); zd.META['ok'].clear(); zd.META['notes'].clear(); asked = []
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                    mock.patch.object(zd, 'previous', lambda n: asked.append(n)), \
                    mock.patch.object(zd, 'build_indeksy', side_effect=AssertionError('bez kluczy nie budować')):
                zd.main()
            self.assertNotIn('indeksy', saved); self.assertNotIn('indeksy', asked, 'bez kluczy nie pytać o poprzedni plik')
            self.assertTrue(any(n.startswith('brak EODHD_KEY — indeksy świata wyłączone') for n in zd.META['notes']))
        finally:
            for s in stubs:
                s.stop()

    def test_page_symbols_and_workflow_secrets(self):
        here = os.path.dirname(os.path.abspath(__file__))
        html = open(os.path.join(here, 'index.html'), encoding='utf-8').read()
        m = html[html.index('const IX_META={'):]
        m = m[:m.index('};')]
        self.assertEqual(dict(_re_v106.findall(r"([A-Z0-9]+):\['([a-z]{2})'", m)), {**{s: c for s, c, _ in zd.IX_SYMBOLS}, **{s: c for s, c, _, _ in zd.IX_FMP}}, 'te same kody i flagi na stronie i w zbieraczu (EODHD + FMP)')
        self.assertIn("srvJSON('indeksy')", html)
        self.assertEqual(len(set(s for s, _, _ in zd.IX_SYMBOLS)), len(zd.IX_SYMBOLS))
        self.assertTrue(all(0 <= h <= 23 for _, _, h in zd.IX_SYMBOLS))
        self.assertEqual(zd.IX_ETF[:len(zd.DAY_SYMS)], tuple(zd.DAY_SYMS), 'fundusze regionów mapy pierwsze (limit zapasu Tiingo)')
        self.assertEqual(len(zd.IX_ETF), len(set(zd.IX_ETF)))
        wf = open(os.path.join(here, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        for k in zd.IX_KEYS:
            self.assertRegex(wf, r'\n\s+' + k + r': \$\{\{ secrets\.[A-Z_]+( \|\| secrets\.' + k + r')? \}\}', k)   # nazwa sekretu właściciela z zapasem *_KEY (26.09)
        self.assertNotIn('EODHD', html[html.index('const EXTRA100='):html.index('\n', html.index('const EXTRA100='))], 'nazwa dostawcy nie w słowniku panelu')


class DzwigniaV109(unittest.TestCase):
    # v109 (dzwignia2): Kraken Futures, Coinbase International, dYdX — okresy finansowania z dokumentacji (co godzinę), stawka bezwzględna Kraken → względna,
    # wiersze zepsute → brak (nie zero), suma giełd z bieżącym stanem (do 6 h, bez Binance), dobieranie nowych części do pliku sprzed v109.
    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()
        self.V = DzwigniaV104('test_num_never_zero_for_missing')   # nagrania i trasy odpowiedzi bloku v104 (z nowymi giełdami)

    def test_iso_and_row_normalisation(self):
        self.assertEqual(zd.lev_iso('2026-09-26T07:13:30.453Z'), '2026-09-26T07:13:30+00:00'); self.assertEqual(zd.lev_iso('2026-09-26T07:13:30+00:00'), '2026-09-26T07:13:30+00:00')
        for v in (None, '', 'wczoraj', '2026-09-26', 1790400000):
            self.assertIsNone(zd.lev_iso(v), repr(v))
        r = zd.lev_row(0.0001, 8, 10.0, 100.0, 5.0, 500.0, 't1')   # stawka za 8 h → godzinowa / 8, rocznie × 24 × 365 × 100
        self.assertEqual(r['f_h'], round(0.0001 / 8, 12)); self.assertEqual(r['f_y'], round(0.0001 / 8 * 24 * 365 * 100, 3)); self.assertEqual(r['f_hours'], 8)
        self.assertEqual((r['oi'], r['oi_usd'], r['px'], r['vol'], r['vol_usd'], r['t']), (10.0, 1000, 100.0, 5.0, 500, 't1'))
        r = zd.lev_row(None, 1, 10.0, None)
        self.assertEqual((r['f_h'], r['f_y'], r['oi'], r['oi_usd'], r['px']), (None, None, 10.0, None, None), 'bez stawki i ceny: pozycje w monetach zostają, USD brak'); self.assertEqual(r['t'], zd.NOW)
        self.assertIsNone(zd.lev_row(None, 1, None, 100.0), 'bez pozycji i bez stawki — brak wiersza')
        self.assertIsNone(zd.lev_row(0.5, 1, 1.0, 100.0)['f_h'], 'stawka 50 %/h = pomyłka jednostki → brak, nie liczba')
        self.assertIsNone(zd.lev_row(0.0001, 0, 1.0, 100.0)['f_h'], 'okres 0 → brak stawki')
        self.assertIsNone(zd.lev_row(0.0001, 1, 0, 100.0)['oi'], 'zero pozycji = nie do odróżnienia od braku → brak')
        self.assertIsNone(zd.lev_row(0.0001, 1, 1.0, 100.0, 0.0, 0.0)['vol_usd'])

    def test_kraken_absolute_to_relative_hourly_and_bad_rows(self):
        o = zd.kr_parse(self.V.KR)
        self.assertEqual(o['t'], '2026-09-26T07:13:30+00:00'); self.assertEqual(o['f_hours'], 1); self.assertEqual(sorted(o), ['BTC', 'ETH', 'f_hours', 't'], 'PI_, SOL bez liczby, bez symbolu, śmieć, zawieszony — pominięte')
        b = o['BTC']; fh = -0.2528880558106387 / 84005.90053859816
        self.assertEqual(b['sym'], 'PF_XBTUSD'); self.assertEqual(b['f_h'], round(fh, 12), 'stawka bezwzględna (USD za kontrakt za godzinę) / cena znacznikowa = względna godzinowa')
        self.assertEqual(b['f_y'], round(fh * 24 * 365 * 100, 3)); self.assertAlmostEqual(b['f_y'], -2.637, 2)
        self.assertEqual(b['oi'], 2175.3188); self.assertEqual(b['oi_usd'], round(2175.3188 * 84005.90053859816)); self.assertEqual(b['px'], 84005.90053859816)
        self.assertEqual((b['vol'], b['vol_usd'], b['t']), (3819.3305, 321113985, '2026-09-26T07:13:30+00:00'))
        self.assertGreater(o['ETH']['f_y'], 0); self.assertEqual(o['ETH']['oi_usd'], round(26150.793 * 2690.15637870521))
        j = {'serverTime': 'x', 'tickers': [{'symbol': 'PF_XBTUSD', 'markPrice': 80000.0, 'openInterest': 1.0, 'fundingRate': -0.25, 'relativeFundingRate': -2.5e-06}]}
        r = zd.kr_parse(j, {'BTC': 'PF_XBTUSD'})
        self.assertEqual(r['BTC']['f_h'], -2.5e-06, 'pole względne ma pierwszeństwo, gdy giełda je podaje'); self.assertEqual(r['t'], zd.NOW, 'zły czas serwera → czas przebiegu')
        j = {'tickers': [{'symbol': 'PF_XBTUSD', 'markPrice': 80000.0, 'openInterest': 'abc', 'fundingRate': -0.25}, {'symbol': 'PF_ETHUSD', 'openInterest': 3.0, 'fundingRate': 0.01}]}
        r = zd.kr_parse(j)
        self.assertEqual((r['BTC']['oi'], r['BTC']['oi_usd'], r['BTC']['f_h']), (None, None, round(-0.25 / 80000.0, 12)), 'pozycje bez liczby → brak, stawka zostaje')
        self.assertEqual((r['ETH']['oi'], r['ETH']['oi_usd'], r['ETH']['f_h'], r['ETH']['px']), (3.0, None, None, None), 'bez ceny znacznikowej: brak USD i brak stawki względnej (nie zero)')
        for bad in ({'tickers': []}, {'tickers': [{'symbol': 'PF_SOLUSD', 'markPrice': 1.0, 'openInterest': 1.0}]}, [], {'tickers': [{'symbol': 'PF_XBTUSD', 'suspended': True, 'markPrice': 1.0, 'openInterest': 1.0}]}):
            with self.assertRaises(ValueError):
                zd.kr_parse(bad)
        with mock.patch.object(zd, 'get_json', return_value={'serverTime': 'x', 'tickers': [{'symbol': 'PF_XBTUSD', 'markPrice': 80000.0, 'openInterest': 1.0, 'fundingRate': -0.25}]}):
            r = zd.lev_kr({'ETH': {'oi': 9.0, 't': 'stary'}})
        self.assertEqual(r['ETH'], {'oi': 9.0, 't': 'stary'}, 'brak wiersza ETH → poprzednia moneta z własnym czasem')
        self.assertTrue(any(e.startswith('Dźwignia: Kraken: brak wiersza PF_ETHUSD') for e in zd.META['errors']), zd.META['errors'])

    def test_coinbase_interval_from_field_and_quote_fallback(self):
        r = zd.cb_parse(self.V.CB['BTC'], 'BTC')
        self.assertEqual(r['f_hours'], 1.0, 'funding_interval 3 600 000 000 000 ns = 1 h'); self.assertEqual(r['f_h'], 8e-06); self.assertEqual(r['f_y'], round(8e-06 * 24 * 365 * 100, 3))
        self.assertEqual((r['oi'], r['oi_usd'], r['px'], r['vol'], r['vol_usd'], r['t']), (1075.7102, round(1075.7102 * 84012.9), 84012.9, 43932.0549, 3692548262, '2026-09-26T07:13:31+00:00'))
        j = dict(self.V.CB['ETH']); del j['funding_interval']
        self.assertEqual(zd.cb_parse(j, 'ETH')['f_hours'], 1, 'brak pola → okres z dokumentacji (1 h)')
        self.assertEqual(zd.cb_parse(dict(self.V.CB['ETH'], funding_interval='28800000000000'), 'ETH')['f_hours'], 8.0, 'pole 8 h → stawka dzielona przez 8')
        self.assertEqual(zd.cb_parse(dict(self.V.CB['ETH'], funding_interval='-5'), 'ETH')['f_hours'], 1)
        q = dict(self.V.CB['BTC']['quote'], predicted_funding=None)
        r = zd.cb_parse(dict(self.V.CB['BTC'], quote=q), 'BTC'); self.assertIsNone(r['f_h']); self.assertEqual(r['oi'], 1075.7102, 'brak stawki → None, pozycje zostają')
        r = zd.cb_parse(dict(self.V.CB['BTC'], open_interest='x'), 'BTC'); self.assertIsNone(r['oi']); self.assertIsNone(r['oi_usd']); self.assertEqual(r['f_h'], 8e-06)
        with self.assertRaises(ValueError):
            zd.cb_parse(dict(self.V.CB['BTC'], open_interest=None, quote=dict(q, predicted_funding='')), 'BTC')
        with self.assertRaises(ValueError):
            zd.cb_parse([], 'BTC')
        calls = []

        def gj(url, headers=None, timeout=30):
            calls.append(url); self.assertLessEqual(timeout, 20)
            if url.endswith('/quote'):
                return self.V.CB['BTC']['quote']
            if 'ETH-PERP' in url:
                raise zd.urllib.error.HTTPError(url, 503, 'x', {}, None)
            return {k: v for k, v in self.V.CB['BTC'].items() if k != 'quote'}
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.lev_cb({'ETH': {'oi': 1.0, 't': 'stary'}})
        self.assertEqual([c.split('/instruments/')[1] for c in calls], ['BTC-PERP', 'BTC-PERP/quote', 'ETH-PERP'], 'szczegóły bez notowania → osobne zapytanie o notowanie')
        self.assertEqual(o['BTC']['oi_usd'], round(1075.7102 * 84012.9)); self.assertEqual(o['ETH'], {'oi': 1.0, 't': 'stary'}, 'moneta z błędem → poprzednia z własnym czasem'); self.assertEqual(o['f_hours'], 1)
        self.assertTrue(any(e.startswith('Dźwignia: Coinbase: ETH: HTTP Error 503') for e in zd.META['errors']), zd.META['errors'])
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('offline')):
            with self.assertRaises(ValueError):
                zd.lev_cb({'BTC': {'oi': 1.0}})   # nic nowego — część nieudana (zachowanie poprzedniej części zapada wyżej)

    def test_dydx_hourly_rate_base_units_and_missing_market(self):
        r = zd.dy_parse(self.V.DY['BTC'], 'BTC')
        self.assertEqual(r['f_hours'], 1); self.assertEqual(r['f_h'], round(-0.00000078846153846154, 12)); self.assertEqual(r['f_y'], round(-0.00000078846153846154 * 24 * 365 * 100, 3))
        self.assertEqual((r['oi'], r['oi_usd'], r['px'], r['vol'], r['vol_usd'], r['t']), (190.2829, round(190.2829 * 83979.10208), 83979.10208, None, 2793172, zd.NOW))
        with self.assertRaises(ValueError):
            zd.dy_parse(self.V.DY['ETH'], 'BTC')   # inny rynek niż proszony
        with self.assertRaises(ValueError):
            zd.dy_parse({'markets': {'BTC-USD': dict(self.V.DY['BTC']['markets']['BTC-USD'], status='PAUSED')}}, 'BTC')
        with self.assertRaises(ValueError):
            zd.dy_parse({'markets': {'BTC-USD': {'ticker': 'BTC-USD', 'oraclePrice': 'x', 'openInterest': None, 'nextFundingRate': 'nan'}}}, 'BTC')
        r = zd.dy_parse({'markets': {'ETH-USD': {'oraclePrice': '2688.87', 'openInterest': 'abc', 'nextFundingRate': '0.00001'}}}, 'ETH')
        self.assertEqual((r['oi'], r['oi_usd'], r['f_h']), (None, None, 1e-05), 'pozycje bez liczby → brak, stawka zostaje')

        def gj(u, headers=None, timeout=30):
            if 'BTC' in u:
                return self.V.DY['BTC']
            raise RuntimeError('offline')
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.lev_dy(None)
        self.assertEqual(o['BTC']['oi'], 190.2829); self.assertNotIn('ETH', o); self.assertTrue(any(e.startswith('Dźwignia: dYdX: ETH: offline') for e in zd.META['errors']), zd.META['errors'])

    def test_all_sum_excludes_stale_missing_zero_and_binance(self):
        now = datetime.datetime.fromisoformat(zd.NOW); fresh = zd.NOW; old = (now - datetime.timedelta(hours=7)).isoformat()
        out = {'ok': {'hl': True, 'okx': True, 'kr': False, 'cb': True, 'dy': True, 'bn': True}, 'part_at': {'hl': fresh, 'okx': old, 'kr': old, 'cb': fresh, 'dy': fresh, 'bn': fresh},
               'hl': {'rows': {'BTC': {'oi_usd': 3e9}, 'ETH': {'oi_usd': None}}}, 'okx': {'BTC': {'oi_usd': 2e9, 't': fresh}, 'ETH': {'oi_usd': 1e9, 't': old}},   # wiersz ma własny czas: OKX BTC świeży mimo starej części
               'kr': {'BTC': {'oi_usd': 1e8, 't': old}, 'ETH': {'oi_usd': 5e7}}, 'cb': {'BTC': {'oi_usd': 0, 't': fresh}, 'ETH': {'oi_usd': 4e7, 't': fresh}}, 'dy': {'BTC': {'oi_usd': 'x', 't': fresh}},
               'bn': {'day': '2026-09-25', 'BTC': {'last': {'oi_usd': 8e9}}}}
        self.assertEqual(zd.lev_all(out), {'BTC': {'usd': 5000000000, 'v': 'hl,okx'}, 'ETH': {'usd': 40000000, 'v': 'cb'}}, 'stare (> 6 h), brak, zero, tekst i Binance — poza sumą; lista giełd posortowana')
        self.assertEqual(zd.lev_all({'ok': {}, 'part_at': {}, 'bn': out['bn']}), {}, 'bez giełd z bieżącym stanem — brak sumy, nie zero')
        self.assertEqual(zd.lev_all(out, now + datetime.timedelta(hours=7)), {}, 'siedem godzin później wszystko za stare')

    def test_build_seven_parts_history_total_and_only_new_parts(self):
        self.enterContext(mock.patch.object(zd, 'NOW', '2026-09-26T07:30:00+00:00'))   # nagrania z 07:13 UTC: suma „wszystkie giełdy” bierze wiersze < 6 h — test nie może zależeć od zegara (26.09 po 13:13 UTC blokowałby budowę)
        V = self.V
        gb = lambda url, headers=None, timeout=60: V.zip_bytes(V.CSV.replace('BTCUSDT', 'ETHUSDT') if 'ETHUSDT' in url else V.CSV)
        with mock.patch.object(zd, 'hl_post', side_effect=V.hl_post), mock.patch.object(zd, 'get_bytes', side_effect=gb), mock.patch.object(zd, 'get_json', side_effect=V.get_json):
            o = zd.build_dzwignia(None, today=V.TODAY)
        self.assertEqual(o['ok'], {k: True for k in ('hl', 'bn', 'dr', 'okx', 'kr', 'cb', 'dy')}); self.assertEqual(sorted(o['part_at']), sorted(zd.LEV_PX)); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(o['kr']['BTC']['sym'], 'PF_XBTUSD'); self.assertEqual(o['cb']['ETH']['oi'], 17069.0224); self.assertEqual(o['dy']['ETH']['f_hours'], 1); self.assertEqual(o['cb']['t'], zd.NOW)
        exp = round(37688.30372 * 83895.0) + 2395706136 + round(2175.3188 * 84005.90053859816) + round(1075.7102 * 84012.9) + round(190.2829 * 83979.10208)
        self.assertEqual(o['hist'][-1]['all_btc'], exp, 'suma z pięciu giełd z bieżącym stanem (bez Binance)'); self.assertEqual(o['hist'][-1]['all_btc_v'], 'cb,dy,hl,kr,okx'); self.assertIn('all_eth', o['hist'][-1])
        self.assertNotIn('all_btc', o['hist'][0], 'wiersz dnia pliku Binance bez sumy')
        # plik sprzed v109 (cztery części, młody — sprzed 3 h): dobierane tylko nowe części, zdrowe zostają z własnym czasem i wchodzą do sumy w historii
        t0 = (datetime.datetime.fromisoformat(zd.NOW) - datetime.timedelta(hours=3)).isoformat()
        prev = {'at': t0, 'full_at': t0, 'ok': {'hl': True, 'bn': True, 'dr': True, 'okx': True}, 'part_at': {'hl': t0, 'bn': t0, 'dr': t0, 'okx': t0},
                'hl': {'rows': {'BTC': {'oi_usd': 3e9}}, 'top': ['BTC']}, 'bn': {'day': '2026-09-25', 'BTC': {'last': {'oi_usd': 8e9}}}, 'dr': {'BTC': {'dvol': {'v': 30.0, 't': t0}}}, 'okx': {'BTC': {'t': t0, 'oi_usd': 2e9}},
                'hist': [{'d': V.TODAY.isoformat(), 'hl_btc': 3e9}]}

        def gj(u, headers=None, timeout=30):
            self.assertTrue(any(h in u for h in ('kraken', 'coinbase', 'dydx')), 'zdrowej części nie pobieramy: ' + u); return V.get_json(u, headers, timeout)
        with mock.patch.object(zd, 'hl_post', side_effect=AssertionError('zdrowej części nie pobieramy')), mock.patch.object(zd, 'get_bytes', side_effect=AssertionError('nie')), mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.build_dzwignia(prev, today=V.TODAY, only={'kr', 'cb', 'dy'})
        self.assertEqual(o['ok'], {'hl': True, 'bn': True, 'dr': True, 'okx': True, 'kr': True, 'cb': True, 'dy': True}); self.assertEqual(o['full_at'], t0, 'dobranie części nie odświeża pełnej budowy')
        self.assertEqual({k: o['part_at'][k] for k in ('hl', 'okx')}, {'hl': t0, 'okx': t0}); self.assertEqual(o['part_at']['kr'], zd.NOW)
        self.assertEqual(o['hist'][-1]['hl_btc'], 3e9); self.assertEqual(o['hist'][-1]['all_btc_v'], 'cb,dy,hl,kr,okx', 'części sprzed 3 h wciąż w sumie')
        self.assertEqual(o['hist'][-1]['all_btc'], round(3e9 + 2e9 + round(2175.3188 * 84005.90053859816) + round(1075.7102 * 84012.9) + round(190.2829 * 83979.10208)))
        self.assertEqual(zd.META['errors'], [])

    def test_new_part_failures_keep_previous_with_own_time(self):
        V = self.V; t1 = '2026-09-26T06:00:00+00:00'
        prev = {'at': t1, 'ok': {'kr': True, 'cb': True, 'dy': True}, 'part_at': {'kr': t1, 'cb': t1, 'dy': t1}, 'kr': {'t': t1, 'BTC': {'oi_usd': 1.0, 't': t1}},
                'cb': {'t': t1, 'BTC': {'oi_usd': 2.0, 't': t1}, 'ETH': {'oi_usd': 3.0, 't': t1}}, 'dy': {'t': t1, 'BTC': {'oi_usd': 4.0, 't': t1}}}

        def gj(u, headers=None, timeout=30):
            if 'kraken' in u:
                raise zd.urllib.error.HTTPError(u, 503, 'Service Unavailable', {}, None)
            if 'ETH-PERP' in u:
                raise TimeoutError('timed out')
            if 'dydx' in u:
                return {'markets': {}}
            return V.get_json(u, headers, timeout)
        with mock.patch.object(zd, 'hl_post', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'get_json', side_effect=gj):
            o = zd.build_dzwignia(prev, today=V.TODAY)
        self.assertEqual({k: o['ok'][k] for k in ('kr', 'cb', 'dy')}, {'kr': False, 'cb': True, 'dy': False})
        self.assertEqual(o['kr'], prev['kr']); self.assertEqual(o['part_at']['kr'], t1, 'Kraken 503 → poprzednia część z własnym czasem')
        self.assertEqual(o['cb']['BTC']['oi'], 1075.7102); self.assertEqual(o['cb']['ETH'], prev['cb']['ETH'], 'Coinbase: BTC nowe, ETH z poprzedniego przebiegu z własnym czasem'); self.assertEqual(o['part_at']['cb'], zd.NOW)
        self.assertEqual(o['dy'], prev['dy']); self.assertEqual(o['part_at']['dy'], t1, 'dYdX bez rynków → część nieudana, poprzednia zostaje')
        E = zd.META['errors']
        self.assertTrue(any(e.startswith('Dźwignia: Kraken: HTTP Error 503') for e in E), E); self.assertTrue(any(e.startswith('Dźwignia: Coinbase: ETH: timed out') for e in E), E)
        self.assertTrue(any(e == 'Dźwignia: dYdX: BTC: brak rynku; ETH: brak rynku' for e in E), E)

    def test_main_first_run_after_upgrade_fetches_only_new_parts(self):
        saved, calls = {}, []
        stubs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in ('build_aukcje', 'build_instytucje', 'build_krypto', 'build_tic', 'build_bis', 'build_cftc', 'build_cm', 'build_rezerwy', 'build_stopy', 'build_kursy', 'build_obce', 'build_eer', 'build_cofer', 'build_bilans', 'build_safe', 'build_ue', 'build_kanada', 'build_korea', 'build_spw', 'build_meksyk', 'build_fundusze', 'build_surowce', 'build_energia', 'build_usa_makro', 'build_bilans_usa', 'build_oecd', 'build_rynki', 'build_stres', 'build_wieloryby', 'build_indeksy', 'build_ceny_krypto', 'build_insider')]
        env = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY', 'SITE_URL', 'CACHE_DIR')}
        prev = {'at': _iso(10), 'full_at': _iso(10), 'ok': {'hl': True, 'bn': True, 'dr': True, 'okx': True}, 'hl': {'rows': {}}}   # plik sprzed v109: młody, bez nowych części
        built = {'at': zd.NOW, 'ok': {'hl': True, 'bn': True, 'dr': True, 'okx': True, 'kr': True, 'cb': False, 'dy': True}}
        [p.start() for p in stubs]
        try:
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                 mock.patch.object(zd, 'previous', lambda name: prev if name == 'dzwignia' else None), mock.patch.object(zd, 'build_dzwignia', side_effect=lambda p, only=None: calls.append((p, only)) or built):
                zd.main()
            self.assertEqual(calls, [(prev, {'kr', 'cb', 'dy'})], 'młody plik bez nowych części — dobierane tylko one')
            self.assertIs(saved['dzwignia'], built); self.assertIs(zd.META['ok']['dzwignia'], True); self.assertIs(zd.META['ok']['dzwignia_cb'], False); self.assertIs(zd.META['ok']['dzwignia_kr'], True); self.assertIs(zd.META['ok']['dzwignia_hl'], True)
        finally:
            [p.stop() for p in stubs]

    def test_page_and_constants(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'), encoding='utf-8').read()
        self.assertIn("LEV_PARTS=['hl','bn','dr','okx','kr','cb','dy']", html); self.assertIn("LEV_LIVE=['hl','okx','kr','cb','dy'],LEV_SUMAGE=6*3600e3", html); self.assertIn('function levAll(){', html)
        self.assertEqual(zd.LEV_LIVE, ['hl', 'okx', 'kr', 'cb', 'dy']); self.assertEqual(zd.LEV_SUMAGE, 6 * 3600); self.assertEqual(zd.LEV_HOURS, {'kr': 1, 'cb': 1, 'dy': 1})
        self.assertEqual(sorted(zd.LEV_PX), ['bn', 'cb', 'dr', 'dy', 'hl', 'kr', 'okx']); self.assertEqual(zd.LEV_TIMEOUT, 20)


class Wieloryby2V108(WielorybyV105):
    '''v108: więcej giełd (Bybit, KuCoin, Bitfinex) — listy z raportów dowodu rezerw giełd; sumy sald i noty per giełda, brak ≠ zero,
    nowa giełda bez wcześniejszego zrzutu, para „ta sama kwota w obie strony” także na nowej giełdzie, grupy tematów eth_getLogs.'''
    BY1 = '0x695f7dea85bf8c0aaafef0a9484e74834e28ce8b'   # Bybit (z raportu 26.08.2026)
    KC1 = '0xbee64116bd2b1b6373273d01664fbc5532dad06d'   # KuCoin (z raportu 31.08.2026)
    BF1 = '0x742d35cc6634c0532925a3b844bc454e4438f44e'   # Bitfinex (zimny, lista 11.2022)

    def test_ksztalt_list_gield(self):
        g = zd.WH_GIELDY
        self.assertEqual(list(g), ['Binance', 'OKX', 'Bybit', 'KuCoin', 'Bitfinex'])
        self.assertEqual((len(g['Bybit']['addr']), len(g['KuCoin']['addr']), len(g['Bitfinex']['addr'])), (108, 33, 4))
        for n in ('Bybit', 'KuCoin', 'Bitfinex'):
            self.assertEqual(g[n]['tokeny'], ['USDT', 'USDC', 'ETH']); self.assertTrue(g[n]['src'] and g[n]['url'].startswith('https://') and g[n]['since'])
            self.assertEqual(len(set(a.lower() for a in g[n]['addr'])), len(g[n]['addr']), 'bez powtórzeń: ' + n)
        self.assertEqual((g['Bybit']['since'], g['KuCoin']['since'], g['Bitfinex']['since']), ('2026-08-26', '2026-08-31', '2022-11'))
        W = self.W
        self.assertEqual((W[self.BY1], W[self.KC1], W[self.BF1]), ('Bybit', 'KuCoin', 'Bitfinex'))
        self.assertEqual(len(set(W.values())), 5); self.assertEqual(len(W), 164, 'adres należy do jednej giełdy')

    def test_nowe_gieldy_salda_noty_i_pierwszy_zrzut(self):
        '''Pełny przebieg: sumy nowych giełd, wpisy gieldy[...] (n, since, tokeny), historia zaczyna się od pierwszego odczytu;
        poprzedni plik bez nowych giełd zachowuje historię Binance/OKX i nie dopisuje im niczego.'''
        post, calls = self._rpc()
        prev = {'at': '2026-09-25T00:00:00+00:00', 'hist': {'Binance': [['2026-09-25', '2026-09-25T00:05:00+00:00', 1.0, 2.0, 3.0]]},
                'salda': {'Binance': {'eth': 1.0, 'usdt': 2.0, 'usdc': 3.0, 'usd': 9.0, 'blk': 1, 't': '2026-09-25T00:00:00+00:00', 'n': 9}}}
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez zapasu kursu')):
            o = zd.build_wieloryby(prev)
        self.assertEqual(o['ok'], {'cena': True, 'salda': True, 'transfery': True}); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(sorted(o['salda']), ['Binance', 'Bitfinex', 'Bybit', 'KuCoin', 'OKX'])
        b = o['salda']['Bybit']
        self.assertAlmostEqual(b['eth'], 108 * 1.5); self.assertAlmostEqual(b['usdt'], 108 * 2_000_000); self.assertEqual((b['n'], b['blk']), (108, 10000))
        self.assertAlmostEqual(o['salda']['KuCoin']['eth'], 33 * 1.5); self.assertAlmostEqual(o['salda']['Bitfinex']['usdc'], 4 * 2_000_000)
        self.assertAlmostEqual(o['salda']['Bitfinex']['usd'], 4 * 1.5 * 2688.8695031 + 4 * 4_000_000, places=2)
        for n, k in (('Bybit', 108), ('KuCoin', 33), ('Bitfinex', 4)):
            self.assertEqual((o['gieldy'][n]['n'], o['gieldy'][n]['tokeny']), (k, ['USDT', 'USDC', 'ETH'])); self.assertIn('https://', o['gieldy'][n]['url'])
            self.assertEqual(o['hist'][n], [[o['blk_t'][:10], o['blk_t'], k * 1.5, k * 2_000_000.0, k * 2_000_000.0]], 'historia nowej giełdy zaczyna się od pierwszego odczytu')
        self.assertEqual(o['hist']['Binance'][0], ['2026-09-25', '2026-09-25T00:05:00+00:00', 1.0, 2.0, 3.0]); self.assertEqual(len(o['hist']['Binance']), 2)
        self.assertNotIn('OKX', prev['hist']); self.assertEqual(len(o['hist']['OKX']), 1)
        self.assertEqual(sum(1 for w in o['wallets'] if w['exch'] == 'Bybit'), 108); self.assertTrue(all(w['since'] == '2026-08-26' for w in o['wallets'] if w['exch'] == 'Bybit'))

    def test_nowa_gielda_bez_odpowiedzi_nie_jest_zerem(self):
        '''Jeden portfel Bybit bez odpowiedzi = Bybit bez nowego salda (bez sumy częściowej); bez poprzedniego wpisu — brak wpisu, nie zero.
        Pozostałe giełdy liczone normalnie; błąd nazwany.'''
        bad = lambda a, tok: '0x' if (a == self.BY1 and tok == 'usdt') else self._h(10 ** 6)
        post, _ = self._rpc(bal=bad)
        with mock.patch.object(zd, 'post_json', side_effect=post):
            o = zd.build_wieloryby(None)
        self.assertNotIn('Bybit', o['salda']); self.assertNotIn('Bybit', o['hist']); self.assertEqual(o['ok']['salda'], False)
        self.assertEqual(sorted(o['salda']), ['Binance', 'Bitfinex', 'KuCoin', 'OKX']); self.assertAlmostEqual(o['salda']['KuCoin']['usdt'], 33.0)
        self.assertEqual(len(zd.META['errors']), 1); self.assertIn('salda Bybit: brak odpowiedzi węzła', zd.META['errors'][0])
        self.assertTrue(zd.META['errors'][0].startswith('Wieloryby: '))
        # z poprzednim wpisem Bybit: poprzednie saldo z własnym czasem, historia bez nowego wiersza
        zd.META['errors'].clear()
        prev = dict(o, at='2026-09-26T07:00:00+00:00'); prev['salda'] = dict(o['salda'], Bybit={'eth': 5.0, 'usdt': 6.0, 'usdc': 7.0, 'usd': 1.0, 'blk': 1, 't': '2026-09-25T00:00:00+00:00', 'n': 108})
        prev['hist'] = dict(o['hist'], Bybit=[['2026-09-25', '2026-09-25T00:00:00+00:00', 5.0, 6.0, 7.0]])
        with mock.patch.object(zd, 'post_json', side_effect=post):
            o2 = zd.build_wieloryby(prev)
        self.assertEqual(o2['salda']['Bybit'], prev['salda']['Bybit']); self.assertEqual(o2['hist']['Bybit'], prev['hist']['Bybit'])

    def test_dekoduj_i_pary_na_nowych_gieldach(self):
        '''Przelew Bybit → KuCoin = dwa wiersze (out/in); wewnętrzny Bybit → Bybit pominięty; para „ta sama kwota w obie strony” na Bybit oznaczona wew.'''
        BY2 = [a for a in zd.WH_GIELDY['Bybit']['addr'] if a != self.BY1][0]
        L = [self._log(zd.WH_USDT, self.BY1, self.KC1, 3_000_000, tx='0x' + '11' * 32), self._log(zd.WH_USDC, self.BY1, BY2, 9_000_000, tx='0x' + '22' * 32),
             self._log(zd.WH_USDT, self.X, self.BF1, 1_500_000, tx='0x' + '33' * 32), self._log(zd.WH_USDC, self.BY1, self.X, 4_000_000, blk=200, tx='0x' + '44' * 32),
             self._log(zd.WH_USDC, self.X, BY2, 4_000_000, blk=210, tx='0x' + '55' * 32)]
        R = zd.wh_dekoduj(L, self.W)
        rows = sorted(R.values(), key=lambda r: (r['tx'], r['dir']))
        self.assertEqual([(r['tx'][:4], r['dir'], r['exch'], r['amt']) for r in rows],
                         [('0x11', 'in', 'KuCoin', 3000000.0), ('0x11', 'out', 'Bybit', 3000000.0), ('0x33', 'in', 'Bitfinex', 1500000.0), ('0x44', 'out', 'Bybit', 4000000.0), ('0x55', 'in', 'Bybit', 4000000.0)])
        zd.wh_pary(rows)
        self.assertEqual([r.get('wew') for r in rows], [None, None, None, True, True], 'para w 10 bloków na Bybit oznaczona; przelew między giełdami nie')

    def test_grupy_tematow(self):
        '''wh_logi: ≤ WH_TOPICS tematów = dwa zapytania; więcej = po dwa na grupę, wszystkie tematy dokładnie raz; grupa z wynikiem null = paczka nieudana.'''
        f = {'fromBlock': '0x1', 'toBlock': '0x2', 'address': [zd.WH_USDT, zd.WH_USDC]}
        tw = [zd.wh_topic(w['addr']) for w in zd.wh_portfele()]
        c = zd.wh_logi(f, tw)
        self.assertEqual(len(c), 2); self.assertEqual(c[0][1][0]['topics'], [zd.WH_TRANSFER, None, tw]); self.assertEqual(c[1][1][0]['topics'], [zd.WH_TRANSFER, tw])
        c = zd.wh_logi(f, tw, maks=50)
        self.assertEqual(len(c), 8, '164 tematy po 50 = 4 grupy × 2 zapytania')
        self.assertEqual([len(x[1][0]['topics'][2]) for x in c[0::2]], [50, 50, 50, 14]); self.assertEqual(sum((x[1][0]['topics'][1] for x in c[1::2]), []), tw)
        self.assertTrue(all(x[1][0]['fromBlock'] == '0x1' and x[1][0]['address'] == [zd.WH_USDT, zd.WH_USDC] for x in c))
        self.assertEqual(zd.wh_logi(f, []), [])
        # pełny przebieg z grupami po 50: 8 zapytań o logi na paczkę w jednym żądaniu, wiersze z każdej grupy
        post, calls = self._rpc()
        with mock.patch.object(zd, 'WH_TOPICS', 50), mock.patch.object(zd, 'post_json', side_effect=post):
            o = zd.build_wieloryby(None)
        lg = [c for c in calls if c[1][0] == 'eth_getLogs']
        self.assertEqual(len(lg), 6); self.assertTrue(all(c[1] == ['eth_getLogs'] * 8 for c in lg)); self.assertEqual(o['ok']['transfery'], True)
        self.assertEqual([r['tx'][:4] for r in o['transfery']], ['0xff', '0xee', '0xaa', '0xbb'], 'te same wiersze co bez grup (Binance/OKX są w pierwszej grupie)')
        # null w jednej z grup: paczka nieudana, ostatni_blok nie idzie dalej
        prev = dict(o, at='2026-09-26T07:00:00+00:00', part_at={'salda': '2026-09-26T07:00:00+00:00', 'transfery': '2026-09-26T06:40:00+00:00'})
        base, _ = self._rpc(head=10100)

        def nul(url, body, timeout=60):
            out = base(url, body, timeout)
            k = [i for i, b in enumerate(body) if b['method'] == 'eth_getLogs']
            if k:
                out[k[-1]] = dict(out[k[-1]], result=None)   # ostatnia grupa bez odpowiedzi
            return out
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'WH_TOPICS', 50), mock.patch.object(zd, 'post_json', side_effect=nul), mock.patch.object(zd.time, 'sleep'):
            o2 = zd.build_wieloryby(prev)
        self.assertEqual((o2['ok']['transfery'], o2['ostatni_blok']), (False, 10000)); self.assertIn('nie jest listą', zd.META['errors'][0])


# klasa v108 dziedziczy tylko pomocnicze (_rpc, _log, _h, setUp) — testy v105 nie mają biegać drugi raz (loader pomija atrybuty niewywoływalne)
for _n in [n for n in dir(WielorybyV105) if n.startswith('test_') and n not in Wieloryby2V108.__dict__]:
    setattr(Wieloryby2V108, _n, None)

# ===================== v110: krypto3d — zdjęcia węzłów sceny CRYPTO (pliki w repo, nie sieć) =====================
class V110ObrazyWezlow(unittest.TestCase):
    """Zdjęcia gaming / giełdy / memecoiny leżą w img/wezly/, są prawdziwymi plikami JPEG i strona się do nich odwołuje."""

    def test_pliki_jpeg_na_miejscu_i_uzyte_na_stronie(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'index.html'), encoding='utf-8') as f:
            html = f.read()
        for name in ('gaming', 'gieldy', 'memecoiny'):
            p = os.path.join(here, 'img', 'wezly', name + '.jpg')
            self.assertTrue(os.path.exists(p), p)
            with open(p, 'rb') as f:
                head = f.read(3)
            self.assertEqual(head, b'\xff\xd8\xff', name + ': nie JPEG')
            self.assertLess(os.path.getsize(p), 200_000, name + ': za duży plik')
            self.assertIn("'img/wezly/%s.jpg'" % name, html)
        with open(os.path.join(here, 'img', 'LICENCJE.txt'), encoding='utf-8') as f:
            self.assertIn('img/wezly/', f.read())


class DzwigniaV109_1(unittest.TestCase):
    """v109.1: budżet czasu budowniczego dźwigni — po jego wyczerpaniu pozostałe części zostają z poprzedniego przebiegu, nigdy zera."""
    def test_deadline_keeps_previous_parts(self):
        zd.META['errors'].clear()
        clock = [1000.0]

        def mono():
            clock[0] += 12.0   # każde spojrzenie na zegar = 12 s (cztery spojrzenia = 48 s > LEV_LIMIT)
            return clock[0]
        prev = {'at': '2026-09-25T10:00:00+00:00', 'part_at': {k: '2026-09-25T10:00:00+00:00' for k in zd.LEV_PX},
                'ok': {k: True for k in zd.LEV_PX}, 'hl': {'rows': {'BTC': {'oi': 1.0}}}, 'bn': {'day': '2026-09-24', 'BTC': {'x': 1}}, 'dr': {'BTC': {'dvol': 30.0}},
                'okx': {'BTC': {'oi': 1.0}}, 'kr': {'BTC': {'oi': 1.0}}, 'cb': {'BTC': {'oi': 1.0}}, 'dy': {'BTC': {'oi': 1.0}}}
        with mock.patch.object(zd.time, 'monotonic', side_effect=mono), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('offline')), \
                mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('offline')), mock.patch.object(zd, 'hl_post', side_effect=RuntimeError('offline')), \
                mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):      # nic nie odpowiedziało → wyjątek, main() zachowuje poprzedni plik
                zd.build_dzwignia(prev, today=datetime.date(2026, 9, 26))
        errs = list(zd.META['errors'])
        self.assertTrue(any('limit czasu przebiegu' in e for e in errs), errs)
        self.assertTrue(any(e.startswith('Dźwignia: Hyperliquid') and 'offline' in e for e in errs), 'pierwsza część padła zwyczajnie: ' + str(errs))
        self.assertLessEqual(clock[0] - 1000.0, 12.0 * 12, 'zegar nie biegnie bez końca')
        zd.META['errors'].clear()

    def test_tmo_shrinks_with_budget(self):
        zd._LEV_TERMIN[0] = None
        self.assertEqual(zd.lev_tmo(), zd.LEV_TIMEOUT)
        with mock.patch.object(zd.time, 'monotonic', return_value=100.0):
            zd._LEV_TERMIN[0] = 100.0 + 7.5
            self.assertEqual(zd.lev_tmo(), 7.5)
            zd._LEV_TERMIN[0] = 100.0 + 0.5
            with self.assertRaises(RuntimeError):
                zd.lev_tmo()
        zd._LEV_TERMIN[0] = None


class SeoV111(unittest.TestCase):
    """v111: pliki dla wyszukiwarek w repozytorium i ich kopiowanie na stronę; kontrola sprawdza ich dostępność."""
    ROOT = os.path.dirname(os.path.abspath(__file__))

    def test_files(self):
        import xml.etree.ElementTree as ET
        r = open(os.path.join(self.ROOT, 'robots.txt'), encoding='utf-8').read()
        self.assertIn('User-agent: *', r); self.assertIn('Allow: /', r); self.assertIn('Sitemap: https://capitalflowai-app.github.io/sitemap.xml', r)
        self.assertEqual(open(os.path.join(self.ROOT, 'google433f7c24524100a9.html'), encoding='utf-8').read(), 'google-site-verification: google433f7c24524100a9.html')
        xml = open(os.path.join(self.ROOT, 'sitemap.xml'), encoding='utf-8').read().replace('LASTMOD', '2026-09-26')
        root = ET.fromstring(xml)
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9', 'x': 'http://www.w3.org/1999/xhtml'}
        urls = root.findall('s:url', ns)
        self.assertEqual(len(urls), 10)
        locs = [u.find('s:loc', ns).text for u in urls]
        self.assertEqual(locs[0], 'https://capitalflowai-app.github.io/'); self.assertIn('https://capitalflowai-app.github.io/?lang=ja', locs)
        for u in urls:
            alts = u.findall('x:link', ns)
            self.assertEqual(len(alts), 11); self.assertEqual(sum(1 for a in alts if a.get('hreflang') == 'x-default'), 1)
            self.assertEqual(u.find('s:lastmod', ns).text, '2026-09-26')

    def test_workflow_copies_and_check_reads(self):
        wf = open(os.path.join(self.ROOT, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        self.assertIn('cp robots.txt google433f7c24524100a9.html _site/', wf)
        self.assertIn('sitemap.xml > _site/sitemap.xml', wf); self.assertIn('LASTMOD', wf)
        k = open(os.path.join(self.ROOT, 'narzedzia', 'kontrola.py'), encoding='utf-8').read()
        for f in ('robots.txt', 'sitemap.xml', 'google433f7c24524100a9.html'):
            self.assertIn(f, k)


class WielorybyEthV112(WielorybyV105):
    """v112: transfery ETH natywne z publicznego API eksploratora (klucz właściciela) — kolejka rotacji, dekodowanie txlist (próg w USD po kursie),
    pełna odpowiedź = ciąg dalszy, błędy pojedyncze vs przerwanie, część 'eth' w pełnym przebiegu, sortowanie tabeli wg USD, klucz maskowany."""
    KEY = 'TAJNY-KLUCZ-ETH'
    BY1 = '0x695f7dea85bf8c0aaafef0a9484e74834e28ce8b'   # Bybit
    BF1 = '0x742d35cc6634c0532925a3b844bc454e4438f44e'   # Bitfinex

    def _tx(self, fr, to, eth, blk=9990, h='0x' + '77' * 32, err='0', ts=None):
        return {'blockNumber': str(blk), 'timeStamp': str(ts if ts is not None else 1_790_400_000 - (10000 - blk) * 12), 'hash': h, 'from': fr, 'to': to,
                'value': str(int(eth * 10 ** 18)), 'isError': err, 'txreceipt_status': '1' if err == '0' else '0'}

    def test_portfele_i_kolejka(self):
        W = zd.wh_portfele(); c = zd.wh_eth_portfele(W)
        self.assertEqual(len(c), 154, '164 − 10 OKX (tylko USDC)'); self.assertTrue(all(w['exch'] != 'OKX' for w in c))
        q = zd.wh_eth_kolejka(c, {})
        self.assertEqual(len(q), zd.WH_ETH_PER_RUN); self.assertEqual([w['addr'] for w in q], sorted(w['addr'] for w in c)[:zd.WH_ETH_PER_RUN], 'bez stanu: stabilnie wg adresu')
        scan = {w['addr']: 100 + i for i, w in enumerate(c)}; scan.pop(c[5]['addr'])
        q = zd.wh_eth_kolejka(c, scan, 3)
        self.assertEqual([w['addr'] for w in q], [c[5]['addr'], c[0]['addr'], c[1]['addr']], 'bez odczytu najpierw, potem najniższy blok')

    def test_dekoduj_eth(self):
        px = 2500.0
        T = [self._tx(self.X, self.B1, 400.0, h='0x' + '01' * 32),               # 400 ETH × 2500 = dokładnie próg → na giełdę
             self._tx(self.B2, self.X, 399.9, h='0x' + '02' * 32),               # poniżej progu
             self._tx(self.B1, self.B2, 5000.0, h='0x' + '03' * 32),             # wewnętrzny Binance → pominięty
             self._tx(self.BY1, self.BF1, 1000.0, h='0x' + '04' * 32, blk=9995),  # Bybit → Bitfinex: dwa wiersze
             self._tx(self.X, self.B1, 900.0, h='0x' + '05' * 32, err='1'),      # nieudana
             dict(self._tx(self.X, self.B1, 900.0, h='0x' + '06' * 32), value='x'),   # bez kwoty — nie zero
             'śmieć', None]
        R = zd.wh_eth_dekoduj(T, self.W, px)
        rows = sorted(R.values(), key=lambda r: (r['tx'], r['dir']))
        self.assertEqual([(r['tx'][:4], r['dir'], r['exch'], r['amt'], r['usd'], r['blk']) for r in rows],
                         [('0x01', 'in', 'Binance', 400.0, 1000000.0, 9990), ('0x04', 'in', 'Bitfinex', 1000.0, 2500000.0, 9995), ('0x04', 'out', 'Bybit', 1000.0, 2500000.0, 9995)])
        self.assertTrue(all(r['token'] == 'ETH' and r['li'] is None and r['t'].endswith('+00:00') for r in rows))
        self.assertEqual(rows[0]['t'], zd.wh_iso(1_790_400_000 - 120))
        self.assertEqual(zd.wh_eth_dekoduj(T, self.W, None), {}, 'bez kursu — nic (próg nieprzeliczalny)'); self.assertEqual(zd.wh_eth_dekoduj(None, self.W, px), {})

    def _api(self, plan):
        """plan: adres → odpowiedź (dict) albo wyjątek; zapisuje adresy zapytań."""
        urls = []

        def gj(url, headers=None, timeout=30):
            urls.append(url)
            self.assertIn('apikey=' + self.KEY, url); self.assertIn('chainid=1&module=account&action=txlist&address=0x', url)
            a = url.split('address=')[1].split('&')[0]
            r = plan.get(a, {'status': '0', 'message': 'No transactions found', 'result': []})
            if isinstance(r, Exception):
                raise r
            return r
        return gj, urls

    def test_wh_eth_rotacja_bledy_i_pelna_odpowiedz(self):
        W = zd.wh_portfele(); c = zd.wh_eth_portfele(W); head = 10000; do = head - zd.WH_ETH_LAG
        first = sorted(w['addr'] for w in c)[:zd.WH_ETH_PER_RUN]
        a0, a1, a2 = first[0], first[1], first[2]
        full = [self._tx(self.X, a2, 0.001, blk=9000 + i // 100, h='0x%064x' % i) for i in range(zd.WH_ETH_OFFSET)]
        plan = {a0: {'status': '1', 'message': 'OK', 'result': [self._tx(self.X, a0, 500.0, blk=9998, h='0x' + 'aa' * 32)]},
                a1: {'status': '0', 'message': 'NOTOK', 'result': 'Max rate limit reached'},
                a2: {'status': '1', 'message': 'OK', 'result': full}}
        gj, urls = self._api(plan)
        with mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd.time, 'sleep') as sl:
            rows, scan, n, errs, prz = zd.wh_eth(self.KEY, W, self.W, head, 2500.0, {}, termin=None)
        self.assertEqual(n, zd.WH_ETH_PER_RUN - 1); self.assertFalse(prz); self.assertEqual(len(errs), 1); self.assertIn('Max rate limit', errs[0])
        self.assertNotIn(a1, scan, 'portfel z błędem bez stanu — wraca na początek kolejki'); self.assertEqual(scan[a0], do)
        self.assertEqual(scan[a2], 9099 - 1, 'pełna odpowiedź: ciąg dalszy od ostatniego zwróconego bloku (ten blok raz jeszcze)')
        self.assertEqual(len(urls), zd.WH_ETH_PER_RUN); self.assertIn(f'startblock={do - zd.WH_ETH_START + 1}&endblock={do}&', urls[0])
        self.assertEqual(sl.call_count, zd.WH_ETH_PER_RUN - 1, 'odstęp między zapytaniami (limit dostawcy)')
        self.assertEqual([(r['exch'], r['amt'], r['usd']) for r in rows.values()], [(self.W[a0], 500.0, 1250000.0)])
        # drugi przebieg: od ostatniego bloku + 1; portfel z błędem pierwszy w kolejce
        gj2, urls2 = self._api({a1: {'status': '1', 'message': 'OK', 'result': []}})
        with mock.patch.object(zd, 'get_json', side_effect=gj2), mock.patch.object(zd.time, 'sleep'):
            rows2, scan2, n2, errs2, prz2 = zd.wh_eth(self.KEY, W, self.W, 10100, 2500.0, {'eth_scan': scan}, termin=None)
        self.assertTrue(urls2[0].split('address=')[1].startswith(a1)); self.assertEqual(errs2, []); self.assertEqual(scan2[a1], 10095)
        nxt = [u for u in urls2 if a0 in u]
        self.assertEqual(len(nxt), 0, 'a0 sprawdzony przed chwilą (blok 9995) nie jest w kolejce 40 najstarszych — 154 portfele bez odczytu mają pierwszeństwo')
        # zaległość > WH_ETH_START: od nowa (bez udawania ciągłości)
        gj3, urls3 = self._api({})
        with mock.patch.object(zd, 'get_json', side_effect=gj3), mock.patch.object(zd.time, 'sleep'):
            zd.wh_eth(self.KEY, W, self.W, 30000, 2500.0, {'eth_scan': {a: 100 for a in first}}, termin=None)
        self.assertIn(f'startblock={30000 - zd.WH_ETH_LAG - zd.WH_ETH_START + 1}&', urls3[0])
        # trzy kolejne błędy = przerwane
        gj4, _ = self._api({a: zd.urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None) for a in first[:3]})
        with mock.patch.object(zd, 'get_json', side_effect=gj4), mock.patch.object(zd.time, 'sleep'):
            rows4, scan4, n4, errs4, prz4 = zd.wh_eth(self.KEY, W, self.W, head, 2500.0, {}, termin=None)
        self.assertTrue(prz4); self.assertEqual((n4, len(errs4), scan4), (0, 3, {}))
        # budżet czasu: 0 s = najwyżej jedno zapytanie… (pierwsze zawsze, sprawdzenie przed każdym następnym)
        gj5, urls5 = self._api({})
        with mock.patch.object(zd, 'get_json', side_effect=gj5), mock.patch.object(zd.time, 'sleep'):
            zd.wh_eth(self.KEY, W, self.W, head, 2500.0, {}, termin=None, budzet=-1)
        self.assertEqual(len(urls5), 0)

    def test_build_z_kluczem(self):
        post, calls = self._rpc()
        W = zd.wh_portfele(); c = zd.wh_eth_portfele(W); first = sorted(w['addr'] for w in c)[:zd.WH_ETH_PER_RUN]; a0 = first[0]
        plan = {a0: {'status': '1', 'message': 'OK', 'result': [self._tx(self.X, a0, 3000.0, blk=9990, h='0x' + '99' * 32), self._tx(a0, self.X, 1.0, blk=9991, h='0x' + '98' * 32)]}}
        gj, urls = self._api(plan)
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd.time, 'sleep'):
            o = zd.build_wieloryby(None, eth_key=self.KEY)
        self.assertEqual(o['ok'], {'cena': True, 'salda': True, 'transfery': True, 'eth': True}); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(o['part_at']['eth'], zd.NOW); self.assertIn('eksploratora', o['src'])
        e = o['eth']; self.assertEqual((e['n'], e['total'], e['sprawdzono'], e['wiersze'], e['per_run']), (40, 154, 40, 1, zd.WH_ETH_PER_RUN)); self.assertEqual(e['lag_min'], 1)
        self.assertEqual(len(o['eth_scan']), 40); self.assertTrue(all(v == 10000 - zd.WH_ETH_LAG for v in o['eth_scan'].values()))
        r0 = o['transfery'][0]
        self.assertEqual((r0['token'], r0['amt'], r0['usd'], r0['dir'], r0['exch'], r0['blk'], r0['li']), ('ETH', 3000.0, round(3000 * 2688.8695031, 2), 'in', self.W[a0], 9990, None))
        self.assertEqual([r['tx'][:4] for r in o['transfery']], ['0x99', '0xff', '0xee', '0xaa', '0xbb'], 'ETH za 8 mln USD na czele — sortowanie wg USD, stablecoiny wg amt')
        self.assertTrue(all(r.get('usd') == r['amt'] for r in o['transfery'][1:]), 'wiersze USDT/USDC dostają usd = amt')
        # drugi przebieg: wiersz ETH zostaje w oknie, kolejne 40 portfeli, stan skanu rośnie; brak kursu = część ETH z poprzednim stanem
        post2, _ = self._rpc(head=10100)
        gj2, urls2 = self._api({})
        with mock.patch.object(zd, 'post_json', side_effect=post2), mock.patch.object(zd, 'get_json', side_effect=gj2), mock.patch.object(zd.time, 'sleep'):
            o2 = zd.build_wieloryby(o, eth_key=self.KEY)
        self.assertEqual(len(o2['eth_scan']), 80); self.assertEqual(o2['transfery'][0]['tx'][:4], '0x99'); self.assertEqual(o2['eth']['n'], 80)
        self.assertTrue(all(a0 not in u for u in urls2), 'rotacja: sprawdzone portfele czekają na resztę')
        post3, _ = self._rpc(head=10200, price_ok=False)
        with mock.patch.object(zd, 'post_json', side_effect=post3), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('brak zapasu kursu')), mock.patch.object(zd.time, 'sleep'):
            o3 = zd.build_wieloryby(o2, eth_key=self.KEY)
        self.assertEqual(o3['ok']['eth'], False); self.assertEqual(o3['eth_scan'], o2['eth_scan']); self.assertEqual(o3['part_at']['eth'], zd.NOW if False else o2['part_at']['eth'])
        self.assertTrue(any('ETH: brak kursu ETH' in x for x in zd.META['errors']))
        # bez klucza: żadnej części eth (jak przed v112), żadnego zapytania do eksploratora
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez klucza — bez eksploratora')):
            o4 = zd.build_wieloryby(None)
        self.assertNotIn('eth', o4['ok']); self.assertNotIn('eth_scan', o4); self.assertNotIn('eksploratora', o4['src'])

    def test_klucz_maskowany_i_przerwanie_to_blad(self):
        post, _ = self._rpc()
        zd.SECRETS.append(self.KEY)
        try:
            gj, _ = self._api({a: RuntimeError('odbito adres z ' + self.KEY) for a in sorted(w['addr'] for w in zd.wh_eth_portfele(zd.wh_portfele()))[:3]})
            with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd.time, 'sleep'):
                o = zd.build_wieloryby(None, eth_key=self.KEY)
        finally:
            zd.SECRETS.remove(self.KEY)
        self.assertEqual(o['ok']['eth'], False); self.assertEqual(len(zd.META['errors']), 1)
        self.assertNotIn(self.KEY, zd.META['errors'][0]); self.assertIn('***', zd.META['errors'][0]); self.assertTrue(zd.META['errors'][0].startswith('Wieloryby: ETH: '))
        self.assertEqual(o['ok']['transfery'], True, 'część USDT/USDC niezależna od ETH')

    def test_dobowe_sumy_i_dedupe(self):
        R = lambda tx, exch, d, tok, amt, usd, t: {'tx': tx, 'exch': exch, 'dir': d, 'token': tok, 'amt': amt, 'usd': usd, 't': t, 'blk': 1}
        rows = [R('0x1', 'Binance', 'in', 'USDT', 5e6, 5e6, '2026-09-25T23:59:00+00:00'), R('0x2', 'Binance', 'out', 'ETH', 400.0, 1e6, '2026-09-26T00:01:00+00:00'),
                R('0x2', 'Bybit', 'in', 'ETH', 400.0, 1e6, '2026-09-26T00:01:00+00:00'), R('0x1', 'Binance', 'in', 'USDT', 5e6, 5e6, '2026-09-25T23:59:00+00:00'),   # duplikat
                {'tx': '0x3', 'exch': 'OKX', 'dir': 'in', 'token': 'USDC', 'amt': 2e6, 't': None}, 'x']
        dob, kl = zd.wh_dobowe(None, None, rows)
        self.assertEqual(dob, {'2026-09-25': {'Binance': {'USDT': {'in': 5e6, 'out': 0.0, 'n': 1}}}, '2026-09-26': {'Binance': {'ETH': {'in': 0.0, 'out': 1e6, 'n': 1}}, 'Bybit': {'ETH': {'in': 1e6, 'out': 0.0, 'n': 1}}}})
        self.assertEqual(kl, {'0x1|None|Binance|in': '2026-09-25', '0x2|None|Binance|out': '2026-09-26', '0x2|None|Bybit|in': '2026-09-26'}, 'wiersz bez czasu pominięty; duplikat raz')
        d2, k2 = zd.wh_dobowe(None, None, [dict(R('0x7', 'Binance', 'in', 'USDT', 2e6, 2e6, '2026-09-26T01:00:00+00:00'), li=1), dict(R('0x7', 'Binance', 'in', 'USDC', 5e6, 5e6, '2026-09-26T01:00:00+00:00'), li=2)])
        self.assertEqual(d2['2026-09-26']['Binance'], {'USDT': {'in': 2e6, 'out': 0.0, 'n': 1}, 'USDC': {'in': 5e6, 'out': 0.0, 'n': 1}}, 'dwa zdarzenia w jednej transakcji = dwa przelewy (v118.1)')
        dob2, kl2 = zd.wh_dobowe(dob, kl, [R('0x2', 'Bybit', 'in', 'ETH', 400.0, 1e6, '2026-09-26T00:01:00+00:00'), R('0x9', 'OKX', 'in', 'USDC', 3e6, 3e6, '2026-09-29T01:00:00+00:00')])
        self.assertEqual(dob2['2026-09-26']['Bybit']['ETH']['n'], 1, 'ten sam przelew w następnym przebiegu nie liczy się drugi raz')
        self.assertEqual(sorted(dob2), ['2026-09-25', '2026-09-26', '2026-09-29'])
        dob3, kl3 = zd.wh_dobowe(dob2, kl2, [R('0x8', 'OKX', 'in', 'USDC', 3e6, 3e6, '2026-09-30T01:00:00+00:00')])
        self.assertEqual(sorted(dob3), ['2026-09-26', '2026-09-29', '2026-09-30'], 'najwyżej WH_DOB_DNI dni'); self.assertNotIn('0x1|None|Binance|in', kl3, 'klucze z usuniętych dni odpadają')
        # pełny przebieg: sumy z wierszy USDT/USDC (z czasem bloku) i z wierszy ETH; przy braku części — sumy z poprzedniego pliku zostają
        post, _ = self._rpc()
        with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez zapasu kursu')):
            o = zd.build_wieloryby(None)
        dn = o['blk_t'][:10]
        self.assertIn(dn, o['dobowe']); self.assertEqual(o['dobowe'][dn]['Binance']['USDT'], {'in': 2500000.0, 'out': 0.0, 'n': 1}); self.assertEqual(o['dobowe'][dn]['OKX']['USDC']['out'], 6200000.0)
        self.assertEqual(len(o['dobowe_klucze']), 4); self.assertEqual(o['dobowe_od'], zd.NOW)
        with mock.patch.object(zd, 'post_json', side_effect=self._rpc(head=10100)[0]):
            o2 = zd.build_wieloryby(dict(o, dobowe_od='2026-09-01T00:00:00+00:00'))
        self.assertEqual(o2['dobowe_od'], '2026-09-01T00:00:00+00:00', 'początek zbierania sum zostaje z poprzedniego pliku')
        with mock.patch.object(zd, 'post_json', side_effect=RuntimeError('offline')), mock.patch.object(zd.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                zd.build_wieloryby(o)

    def test_notatka_eth_maskowana(self):
        post, _ = self._rpc()
        zd.SECRETS.append(self.KEY)
        try:
            first = sorted(w['addr'] for w in zd.wh_eth_portfele(zd.wh_portfele()))[:2]
            gj, _ = self._api({first[0]: RuntimeError('Invalid API Key ' + self.KEY)})   # jeden błąd, reszta OK → notatka, nie błąd
            with mock.patch.object(zd, 'post_json', side_effect=post), mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd.time, 'sleep'):
                o = zd.build_wieloryby(None, eth_key=self.KEY)
        finally:
            zd.SECRETS.remove(self.KEY)
        self.assertEqual(o['ok']['eth'], True); self.assertEqual(zd.META['errors'], []); self.assertEqual(len(zd.META['notes']), 1)
        self.assertNotIn(self.KEY, zd.META['notes'][0]); self.assertIn('***', zd.META['notes'][0])

    def test_polacz_wg_usd(self):
        a = {'tx': '0x1', 'li': 1, 'exch': 'Binance', 'dir': 'in', 'amt': 2e6, 'blk': 50}                       # stary wiersz bez usd
        b = {'tx': '0x2', 'li': None, 'exch': 'Binance', 'dir': 'in', 'amt': 1500.0, 'usd': 4e6, 'blk': 40, 'token': 'ETH'}
        c = {'tx': '0x3', 'li': 2, 'exch': 'OKX', 'dir': 'out', 'amt': 3e6, 'usd': 3e6, 'blk': 60}
        self.assertEqual([r['tx'] for r in zd.wh_polacz([a], [b, c], 0)], ['0x2', '0x3', '0x1'])
        self.assertEqual(zd.wh_usd(a), 2e6); self.assertEqual(zd.wh_usd(b), 4e6); self.assertEqual(zd.wh_usd({'amt': 5, 'usd': True}), 5)

    def test_main_wiring(self):
        src = open(zd.__file__, encoding='utf-8').read()
        self.assertIn("eth_key = os.environ.get('ETHERSCAN_KEY', '').strip()", src); self.assertIn("SECRETS.append(eth_key)", src)
        self.assertIn("wh = build_wieloryby(prev_wh, eth_key=eth_key or None)", src); self.assertIn("META['ok']['wieloryby_eth'] = bool(wh['ok'].get('eth'))", src)
        self.assertIn("brak ETHERSCAN_KEY — transfery ETH natywne w wielorybach wyłączone", src)
        wf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        self.assertRegex(wf, r'ETHERSCAN_KEY: \$\{\{ secrets\.ETHERSCAN \|\| secrets\.ETHERSCAN_KEY \}\}')


class ArchiwumV113(unittest.TestCase):
    """v113 (część B): własne archiwum CSV — format liczb, scalanie idempotentne z liczeniem rewizji, przeniesienie serii tygodniowych
    do przodu, wiersze TIC/CFTC/rentowności/wielorybów/stablecoinów z nagrań, zapis plików i indeks.json, workflow i krok budowy."""
    ROOT = os.path.dirname(os.path.abspath(__file__))

    @classmethod
    def setUpClass(cls):
        import importlib.util, tempfile
        cls.tmp = tempfile.mkdtemp(prefix='archiwum-')
        os.environ['ARCHIWUM_DIR'] = cls.tmp
        spec = importlib.util.spec_from_file_location('archiwum_v113', os.path.join(cls.ROOT, 'narzedzia', 'archiwum.py'))
        cls.a = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.a)

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); self.a.NOTES.clear()

    def test_fmt(self):
        f = self.a.fmt
        self.assertEqual([f(x) for x in (88304342264.55, 2706087.305344, 3.79, 5.0, -0.0, 0.0, None, 12, float('nan'), 'x', True)],
                         ['88304342264.55', '2706087.305344', '3.79', '5', '0', '0', '', '12', '', 'x', 'true'])
        self.assertEqual(self.a.lata_wstecz(datetime.date(2028, 2, 29), 3), datetime.date(2025, 2, 28)); self.assertEqual(self.a.lata_wstecz(datetime.date(2026, 9, 26), 2), datetime.date(2024, 9, 26))

    def test_merge_idempotent_and_revisions(self):
        cols, key = ['date', 'x', 'v'], ['date', 'x']
        rows = [['2026-09-25', 'a', 1.5], ['2026-09-25', 'b', None], ['2026-09-26', 'a', 2]]
        m1, add1, rev1 = self.a.merge({}, rows, cols, key)
        self.assertEqual((len(m1), add1, rev1), (3, 3, 0)); self.assertEqual(m1[('2026-09-25', 'b')], ['2026-09-25', 'b', ''], 'brak = puste pole, nie 0')
        m2, add2, rev2 = self.a.merge(m1, rows, cols, key)
        self.assertEqual((len(m2), add2, rev2), (3, 0, 0), 'drugie uruchomienie tego samego dnia = ta sama liczba wierszy')
        m3, add3, rev3 = self.a.merge(m2, [['2026-09-26', 'a', 2.5], ['2026-09-27', 'a', 3]], cols, key)
        self.assertEqual((len(m3), add3, rev3), (4, 1, 1)); self.assertEqual(m3[('2026-09-26', 'a')][2], '2.5', 'rewizja = najnowsza wartość')
        self.assertIn(('2026-09-25', 'a'), m3, 'wiersze spoza przebiegu zostają')
        with self.assertRaises(ValueError):
            self.a.merge({}, [['2026-09-25', 'a']], cols, key)

    def test_plynnosc_rows(self):
        walcl = {'2026-09-02': 6600000.0, '2026-09-09': 6590000.0}; tga = {'2026-09-02': 800000.0, '2026-09-09': 810000.0}
        rrp = {'2026-09-01': 100.0, '2026-09-03': 120.5, '2026-09-09': 90.0, '2026-09-10': 80.0}
        r = self.a.plynnosc_rows(walcl, tga, rrp, '2026-09-01')
        self.assertEqual([x[0] for x in r], ['2026-09-03', '2026-09-09', '2026-09-10'], 'dzień sprzed pierwszej środy bez wiersza (nie zero)')
        self.assertEqual(r[0], ['2026-09-03', 6600000.0, 800000.0, 120500.0, 6600000.0 - 800000.0 - 120500.0, 'walcl@2026-09-02,tga@2026-09-02'], 'mld → mln, serie tygodniowe przeniesione')
        self.assertEqual(r[2][1:3], [6590000.0, 810000.0]); self.assertEqual(r[2][5], 'walcl@2026-09-09,tga@2026-09-09')
        self.assertEqual(self.a.plynnosc_rows(walcl, tga, rrp, '2026-09-10'), [r[2]], 'od = początek okresu')

    def test_tic_cftc_rent_rows(self):
        t1 = {'Japan': {'2019-12': {'for_lt_total_net': 5.0}, '2020-01': {'for_lt_total_net': 1234.6}, '2020-02': {'for_lt_total_net': None}}, 'All Countries': {'2020-01': {'for_lt_total_net': -7.4}}}
        t2 = {'Japan': {'2020-01': {'us_lt_total_net': 42.0}}}
        r = self.a.tic_rows(t1, t2)
        self.assertEqual(sorted(r), [['2020-01', 'All Countries', -7, 'slt1_for_lt_total_net'], ['2020-01', 'Japan', 42, 'slt2_us_lt_total_net'], ['2020-01', 'Japan', 1235, 'slt1_for_lt_total_net']])
        self.assertEqual(self.a.tic_rows(t1, None), [x for x in r if x[3].startswith('slt1')], 'bez tabeli 2 — tylko tabela 1')
        rec = {}
        for i, (k, pos, _) in enumerate(zd.CFTC_GROUPS):
            rec[f'{pos}_Long_All'] = str(10 + i); rec[f'{pos}_Short_All'] = str(3 + i)
        bad = dict(rec); bad['Dealer_Positions_Long_All'] = ''
        tabs = {zd.CFTC_MARKETS['btc']: {'2026-09-22': rec, '2024-01-02': rec}, zd.CFTC_MARKETS['eth']: {'2026-09-22': bad}, zd.CFTC_MARKETS['eur']: {'2026-09-22': rec}}
        c = self.a.cftc_rows(tabs, '2024-09-26')
        self.assertEqual(len(c), 5 + 4, 'BTC 5 grup, ETH bez dealera (brak liczby = brak wiersza), EUR nie należy do pliku, stary tydzień poza oknem')
        self.assertIn(['2026-09-22', 'BTC', 'dealer', 10, 3, 7], c); self.assertIn(['2026-09-22', 'ETH', 'nonrept', 14, 7, 7], c)
        y = self.a.rent_rows([['2026-09-24', 4.1], ['2026-09-25', 4.2], ['2024-01-01', 3.0]], [['2026-09-25', 2.6], ['2026-09-26', 2.7]], '2026-01-01')
        self.assertEqual(y, [['2026-09-24', 4.1, None, None], ['2026-09-25', 4.2, 2.6, 1.6], ['2026-09-26', None, 2.7, None]], 'różnica tylko przy obu wartościach; brak = None')

    def _wh(self, ok_eth=True, ok_tr=True, px=2500.0, dobowe=True):
        return {'at': '2026-09-26T00:10:00+00:00', 'eth_usd': px, 'ok': {'salda': True, 'transfery': ok_tr, 'eth': ok_eth},
                'gieldy': {'Binance': {'tokeny': ['USDT', 'USDC', 'ETH']}, 'OKX': {'tokeny': ['USDC']}},
                'salda': {'Binance': {'eth': 10.5, 'usdt': 1000000.0, 'usdc': 2.0, 'blk': 500}, 'OKX': {'eth': 0.6, 'usdt': 29.0, 'usdc': 3000000.0, 'blk': 500}},
                'transfery': [{'token': 'USDT', 'amt': 5e6, 'usd': 5e6, 'dir': 'in', 'exch': 'Binance'}, 'x'],   # tabela obcięta — nie służy do sum (v117)
                'dobowe_od': '2026-09-20T10:00:00+00:00', 'ostatni_t': '2026-09-26T00:12:00+00:00', 'luka': False, 'blk_t': '2026-09-26T00:15:00+00:00', 'eth': {'lag_min': 10},
                'dobowe': ({'2026-09-25': {'Binance': {'USDT': {'in': 5e6, 'out': 2e6, 'n': 2}, 'ETH': {'in': 0.0, 'out': 1e6, 'n': 1}}, 'OKX': {'USDC': {'in': 1e6, 'out': 0.0, 'n': 1}}},
                            '2026-09-26': {'Binance': {'USDT': {'in': 9e9, 'out': 0.0, 'n': 1}}}} if dobowe else {})}

    def test_src_wieloryby(self):
        d = datetime.date(2026, 9, 26)
        with mock.patch.object(zd, 'get_json', return_value=self._wh()):
            r = self.a.src_wieloryby(today=d)
        self.assertEqual(sorted(r), [['2026-09-26', 'Binance', 'ETH', 10.5, 26250.0, 0.0, 1e6, -1e6, 500], ['2026-09-26', 'Binance', 'USDC', 2.0, 2.0, 0.0, 0.0, 0.0, 500],
                                     ['2026-09-26', 'Binance', 'USDT', 1000000.0, 1000000.0, 5e6, 2e6, 3e6, 500], ['2026-09-26', 'OKX', 'USDC', 3000000.0, 3000000.0, 1e6, 0.0, 1e6, 500]],
                         'przepływy z sum dobowych za 25.09 (pełna poprzednia doba), nie z obciętej tabeli ani z dnia bieżącego; USDC bez wpisu w obserwowanej dobie = 0 zmierzone')
        with mock.patch.object(zd, 'get_json', return_value=self._wh(ok_eth=False, ok_tr=False, px=None)):
            r = self.a.src_wieloryby(today=d)
        e = [x for x in r if x[2] == 'ETH'][0]; u = [x for x in r if x[2] == 'USDT'][0]
        self.assertEqual(e[4:8], [None, None, None, None], 'bez kursu i bez części ETH: USD i przepływy puste, nie zero'); self.assertEqual(u[5:8], [None, None, None])
        self.a.NOTES.clear()
        with mock.patch.object(zd, 'get_json', return_value=self._wh(dobowe=False)):
            r = self.a.src_wieloryby(today=d)
        self.assertTrue(all(x[5:8] == [None, None, None] for x in r), 'plik bez sum dobowych (sprzed v117): przepływy puste'); self.assertTrue(any('brak sum dobowych' in n for n in self.a.NOTES))
        self.a.NOTES.clear(); fx = self._wh(); fx['ostatni_t'] = '2026-09-25T23:40:00+00:00'; fx['eth'] = {'lag_min': 90}
        with mock.patch.object(zd, 'get_json', return_value=fx):
            r = self.a.src_wieloryby(today=d)
        self.assertTrue(all(x[5:8] == [None, None, None] for x in r), 'skan nie przekroczył północy (stablecoiny) i rotacja ETH nie objęła doby = puste'); self.assertEqual(sum('nie sięga północy' in n for n in self.a.NOTES), 1); self.assertEqual(sum('rotacja' in n for n in self.a.NOTES), 1)
        self.a.NOTES.clear(); fx = self._wh(); fx['luka'] = True
        with mock.patch.object(zd, 'get_json', return_value=fx):
            r = self.a.src_wieloryby(today=d)
        self.assertTrue(all(x[5:8] == [None, None, None] for x in r if x[2] != 'ETH') and any(x[5] is not None for x in r if x[2] == 'ETH'), 'luka w skanie logów = stablecoiny puste, ETH liczone')
        self.a.NOTES.clear(); fx = self._wh(); fx['dobowe_od'] = '2026-09-25T13:00:00+00:00'
        with mock.patch.object(zd, 'get_json', return_value=fx):
            r = self.a.src_wieloryby(today=d)
        self.assertTrue(all(x[5:8] == [None, None, None] for x in r), 'sumy zbierane od środka doby 25.09 = doba niepełna = puste'); self.assertTrue(any('niepełna' in n for n in self.a.NOTES))
        with mock.patch.object(zd, 'get_json', return_value={'at': 'x', 'salda': {}}):
            with self.assertRaises(RuntimeError):
                self.a.src_wieloryby(today=d)

    def test_src_stable(self):
        h = lambda v: '0x' + format(v, 'x').rjust(64, '0')
        with mock.patch.object(zd, 'wh_rpc', return_value=[h(88304342264550000), h(50282984666730000)]) as rpc:
            r = self.a.src_stable(today=datetime.date(2026, 9, 26))
        self.assertEqual(r, [['2026-09-26', 'USDT', 88304342264.55], ['2026-09-26', 'USDC', 50282984666.73]])
        self.assertEqual(rpc.call_args[0][0][0], ('eth_call', [{'to': zd.WH_USDT, 'data': '0x18160ddd'}, 'latest']), 'totalSupply() przez publiczny węzeł')
        with mock.patch.object(zd, 'wh_rpc', return_value=[h(1), '0x']):
            with self.assertRaises(RuntimeError) as cm:
                self.a.src_stable(today=datetime.date(2026, 9, 26))
        self.assertIn('USDC', str(cm.exception))

    def test_run_writes_files_and_index(self):
        import shutil
        arch = os.path.join(self.tmp, 'run1'); shutil.rmtree(arch, ignore_errors=True)
        src = {'rentownosci': lambda: [['2026-09-25', 4.2, 2.6, 1.6], ['2026-09-24', 4.1, None, None]], 'stablecoiny-eth': lambda: (_ for _ in ()).throw(RuntimeError('węzeł milczy TAJNE'))}
        zd.SECRETS.append('TAJNE')
        try:
            idx = self.a.run(src, arch=arch)
        finally:
            zd.SECRETS.remove('TAJNE')
        self.assertTrue(idx['files']['rentownosci']['ok']); self.assertEqual(idx['files']['rentownosci']['rows'], 2); self.assertEqual(idx['files']['rentownosci']['first'], '2026-09-24')
        self.assertFalse(idx['files']['stablecoiny-eth']['ok']); self.assertEqual(idx['errors'], ['stablecoiny-eth: węzeł milczy ***'], 'klucz maskowany także tu')
        self.assertEqual(open(os.path.join(arch, 'rentownosci.csv'), encoding='utf-8').read(), 'date,ust10y,bund10y,spread\n2026-09-24,4.1,,\n2026-09-25,4.2,2.6,1.6\n', 'posortowane, brak = puste, LF')
        self.assertFalse(os.path.exists(os.path.join(arch, 'stablecoiny-eth.csv')), 'źródło z błędem nie tworzy pliku')
        j = json.load(open(os.path.join(arch, 'indeks.json'), encoding='utf-8'))
        self.assertEqual(j['files']['rentownosci']['cols'], self.a.FILES['rentownosci']['cols']); self.assertIn('Archiwum własne', j['credit'])
        # drugi przebieg: rewizja jednej wartości, nowy dzień; licznik rewizji rośnie z poprzedniego indeksu; poprzedni plik z błędem nadal bez pliku
        idx2 = self.a.run({'rentownosci': lambda: [['2026-09-25', 4.25, 2.6, 1.65], ['2026-09-26', 4.3, 2.7, 1.6]]}, arch=arch, prev_index=j)
        r2 = idx2['files']['rentownosci']
        self.assertEqual((r2['rows'], r2['added'], r2['revised'], r2['revisions_total'], r2['first'], r2['last']), (3, 1, 1, 1, '2026-09-24', '2026-09-26'))
        idx3 = self.a.run({'rentownosci': lambda: [['2026-09-26', 4.3, 2.7, 1.6]]}, arch=arch, prev_index=idx2)
        self.assertEqual((idx3['files']['rentownosci']['added'], idx3['files']['rentownosci']['revised'], idx3['files']['rentownosci']['revisions_total']), (0, 0, 1), 'ten sam dzień raz jeszcze = bez zmian')
        # inny nagłówek w istniejącym pliku = plik od nowa, z uwagą
        with open(os.path.join(arch, 'rentownosci.csv'), 'w', encoding='utf-8') as f:
            f.write('date,inne\n2026-01-01,1\n')
        idx4 = self.a.run({'rentownosci': lambda: [['2026-09-26', 4.3, 2.7, 1.6]]}, arch=arch)
        self.assertEqual(idx4['files']['rentownosci']['rows'], 1); self.assertTrue(any('nagłówek' in n for n in idx4['notes']))

    def test_seria_json(self):
        import shutil
        arch = os.path.join(self.tmp, 'seria'); shutil.rmtree(arch, ignore_errors=True)
        idx = self.a.run({'rentownosci': lambda: [['2026-09-24', 4.1, None, None], ['2026-09-25', 4.2, 2.6, 1.6]],
                          'wieloryby': lambda: [['2026-09-26', 'Binance', 'ETH', 10.0, 25000.0, 0.0, 0.0, 0.0, 5], ['2026-09-26', 'Binance', 'USDT', 100.0, 100.0, 0.0, 0.0, 0.0, 5],
                                                ['2026-09-26', 'OKX', 'USDC', 7.0, 7.0, None, None, None, 5], ['2026-09-27', 'Binance', 'ETH', 10.0, None, None, None, None, 6], ['2026-09-27', 'Binance', 'USDT', 100.0, 100.0, 0.0, 0.0, 0.0, 6]],
                          'tic': lambda: [['2026-06', 'All Countries', 100, 'slt1_for_lt_total_net'], ['2026-06', 'Japan', 5, 'slt1_for_lt_total_net'], ['2026-06', 'All Countries', -20, 'slt2_us_lt_total_net']],
                          'cftc-krypto': lambda: [['2026-09-22', 'BTC', 'lev_funds', 10, 12, -2], ['2026-09-22', 'BTC', 'nonrept', 1, 1, 0]],
                          'stablecoiny-eth': lambda: [['2026-09-26', 'USDT', 88.5]],
                          'plynnosc': lambda: [['2026-09-25', 6600000.0, 800000.0, 120500.0, 5679500.0, 'walcl@2026-09-24,tga@2026-09-24']]}, arch=arch)
        self.assertEqual(idx['errors'], [])
        j = json.load(open(os.path.join(arch, 'seria.json'), encoding='utf-8'))
        S = j['series']
        self.assertEqual(S['rent.ust']['d'], [['2026-09-24', 4.1], ['2026-09-25', 4.2]]); self.assertEqual(S['rent.bund']['d'], [['2026-09-25', 2.6]], 'pusta komórka = brak punktu')
        self.assertEqual((S['rent.spread']['unit'], S['rent.spread']['n'], S['rent.ust']['first'], S['rent.ust']['last']), ('pp', 1, '2026-09-24', '2026-09-25'))
        self.assertEqual(S['wh.Binance']['d'], [['2026-09-26', 25100.0]], 'suma USD giełdy; dzień z ETH bez kursu = bez punktu (nie suma częściowa)'); self.assertEqual(S['wh.OKX']['d'], [['2026-09-26', 7.0]])
        self.assertEqual((S['tic.in']['d'], S['tic.out']['d'], S['tic.in']['freq']), ([['2026-06', 100.0]], [['2026-06', -20.0]], 'M'), 'tylko „All Countries”')
        self.assertEqual(S['cftc.btc.lev']['d'], [['2026-09-22', -2.0]]); self.assertEqual(S['cftc.eth.lev']['d'], []); self.assertNotIn('cftc.btc.nonrept', S)
        self.assertEqual((S['stab.usdt']['d'], S['stab.usdc']['d']), ([['2026-09-26', 88.5]], []))
        self.assertEqual(S['plyn.net']['d'], [['2026-09-25', 5679500.0]]); self.assertEqual(S['plyn.rrp']['unit'], 'mln USD')
        self.assertEqual(idx['seria']['rent.ust'], 2); self.assertIn('credit', j)
        big = os.path.join(self.tmp, 'seria-big'); shutil.rmtree(big, ignore_errors=True)
        self.a.run({'rentownosci': lambda: [['2024-%02d-%02d' % (1 + i // 28, 1 + i % 28), 1.0 + i, None, None] for i in range(300)] + [['2025-%02d-%02d' % (1 + i // 28, 1 + i % 28), 2.0, None, None] for i in range(300)]}, arch=big)
        j2 = json.load(open(os.path.join(big, 'seria.json'), encoding='utf-8'))
        self.assertEqual(j2['series']['rent.ust']['n'], self.a.SERIA_N, 'najwyżej SERIA_N ostatnich punktów; pełna historia w CSV'); self.assertTrue(j2['series']['rent.ust']['first'].startswith('2024-'))

    def test_workflow_and_build_step(self):
        wf = open(os.path.join(self.ROOT, '.github', 'workflows', 'archiwum.yml'), encoding='utf-8').read()
        self.assertIn("cron: '20 1 * * *'", wf); self.assertIn('[skip ci]', wf); self.assertIn('FRED_KEY: ${{ secrets.FRED_KEY }}', wf)
        self.assertIn('contents: write', wf); self.assertIn('git add archiwum', wf); self.assertIn('python3 narzedzia/archiwum.py', wf); self.assertIn('workflow_dispatch', wf)
        self.assertNotIn('toJSON(secrets)', wf)
        st = open(os.path.join(self.ROOT, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        self.assertIn('cp -r archiwum _site/archiwum', st)
        src = open(os.path.join(self.ROOT, 'narzedzia', 'archiwum.py'), encoding='utf-8').read()
        for n in self.a.FILES:
            self.assertIn(f"'{n}'", src)
        self.assertEqual(set(self.a.FILES), {'wieloryby', 'stablecoiny-eth', 'plynnosc', 'tic', 'cftc-krypto', 'rentownosci'})
        for spec in self.a.FILES.values():
            self.assertTrue(all(c.isascii() and c == c.lower() for c in spec['cols']), spec['cols']); self.assertTrue(set(spec['key']) <= set(spec['cols']))
        self.assertIn('FRED_OBS', src)
        for bad in ('api.coingecko.com', 'sosovalue.xyz', 'coinpaprika.com', 'finnhub.io', 'twelvedata.com', 'coinmarketcap.com', 'api.etherscan.io', 'cryptopanic.com', 'eodhd.com', 'tiingo.com', 'massive.com', 'polygon.io'):
            self.assertNotIn(bad, src, 'archiwum publiczne tylko z domeny publicznej i obliczeń własnych')


class KontrolaV115(unittest.TestCase):
    """v115 (część C): kontrola jakości — godziny robocze, wiek danych wg kategorii i progi (żółte / czerwone 2×), zgodność kapitalizacji
    z medianą 30 dni, TGA, wieloryby z archiwum, historia 3 przebiegów, format raportu (nagłówek i „Wynik:” bez zmian), straż kluczy, workflow."""
    ROOT = os.path.dirname(os.path.abspath(__file__))

    @classmethod
    def setUpClass(cls):
        import importlib.util, tempfile
        cls.tmp = tempfile.mkdtemp(prefix='kontrola-')
        os.environ['KONTROLA_DIR'] = os.path.join(cls.tmp, 'kontrola'); os.environ['KONTROLA_ARCH'] = os.path.join(cls.tmp, 'archiwum')
        for n, f in (('k', 'kontrola.py'), ('s', 'straz_kluczy.py')):
            spec = importlib.util.spec_from_file_location('v115_' + n, os.path.join(cls.ROOT, 'narzedzia', f))
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); setattr(cls, n, mod)

    def test_godziny_robocze_i_wiek(self):
        k, D = self.k, datetime.datetime
        tz = datetime.timezone.utc
        self.assertEqual(k.godziny_robocze(D(2026, 9, 25, 0, tzinfo=tz), D(2026, 9, 25, 12, tzinfo=tz)), 12.0)                  # piątek
        self.assertEqual(k.godziny_robocze(D(2026, 9, 25, 12, tzinfo=tz), D(2026, 9, 28, 6, tzinfo=tz)), 18.0, 'sobota i niedziela nie liczą się')
        self.assertEqual(k.godziny_robocze(D(2026, 9, 26, 0, tzinfo=tz), D(2026, 9, 27, 23, tzinfo=tz)), 0.0)
        now = D(2026, 9, 26, 6, 20, tzinfo=tz)   # sobota rano
        self.assertEqual(k.wiek_danych('2026-09-24', 'day', 'd', now), 24 * 60, 'dane z czwartku: cały piątek (24 h roboczych), sobota nie')
        self.assertEqual(k.wiek_danych('2026-09-24', 'day', 'w', now), int((now - D(2026, 9, 25, tzinfo=tz)).total_seconds() // 60), 'tygodniowe: zwykłe minuty od końca dnia')
        self.assertEqual(k.wiek_danych('2026-07', 'month', 'm', now), int((now - D(2026, 8, 1, tzinfo=tz)).total_seconds() // 60))
        self.assertEqual(k.wiek_danych('2026-12', 'month', 'm', D(2027, 1, 1, 1, tzinfo=tz)), 60)
        self.assertEqual(k.wiek_danych('2026-09-26T05:20:00+00:00', 'ts', 'h', now), 60); self.assertIsNone(k.wiek_danych('zle', 'day', 'd', now))
        self.assertEqual((k.ocena(100, 180), k.ocena(181, 180), k.ocena(361, 180), k.ocena(None, 180)), ('✅', '⚠️', '❌', '?'))

    def test_swiezosc_wg_plikow(self):
        k, tz = self.k, datetime.timezone.utc
        now = datetime.datetime(2026, 9, 26, 6, 20, tzinfo=tz)
        files = {'rynki': {'part_at': {'fx': '2026-09-26T05:43:00+00:00', 'ust': '2026-09-26T02:00:00+00:00'}},
                 'wieloryby': {'part_at': {'salda': '2026-09-26T01:00:00+00:00'}}, 'dzwignia': None,
                 'instytucje': {'tga': {'asof': '2026-09-24'}}, 'etf': {'asof': '2026-09-25'}, 'energia': {'s': {'wti': {'d': [['2026-09-01', 1]]}}},
                 'fred': {'series': {'RRPONTSYD': {'asof': '2026-09-25'}, 'WALCL': {'asof': '2026-09-16'}}}, 'cftc': {'asof': '2026-09-08'},
                 'tic': {'asof': '2026-04'}, 'oecd': {'cli': {'USA': [['2026-07', 1], ['2026-08', 2]]}, 'irlt': {'DEU': [['2026-08', 1]]}}, 'usa-makro': {'s': {'cpi': {'d': [['2026-06', 1]]}}}}
        rows = {r[0]: r for r in k.swiezosc(files, now)}
        st = {lab: r[1] for lab, r in rows.items()}
        self.assertEqual(st['rynki (kursy EBC, rentowności)'], '✅', 'najnowsza część 37 min temu'); self.assertEqual(rows['rynki (kursy EBC, rentowności)'][3], '2026-09-26T05:43:00+00:00')
        self.assertEqual(st['wieloryby (salda portfeli giełd)'], '⚠️', '5 h 20 min > 3 h'); self.assertEqual(st['dźwignia (giełdy pochodnych)'], '?')
        self.assertEqual(st['TGA (Fiscal Data, dziennie)'], '✅', 'czwartek + piątek roboczy = 24 h < 36 h; sobota nie liczy się')
        self.assertEqual(st['EIA ceny dzienne (publikowane co tydzień)'], '❌', 'dane z 1.09: 24 dni > 2 × 9 dni')
        self.assertEqual(st['CFTC (raport tygodniowy)'], '⚠️', '17 dni > 12'); self.assertEqual(st['FRED tygodniowe (WALCL)'], '⚠️', '9,3 dnia > 9')
        self.assertEqual(st['TIC (miesięcznie)'], '⚠️', 'kwiecień: 148 dni > 75, < 150'); self.assertEqual(st['OECD (miesięcznie)'], '✅'); self.assertEqual(st['BLS (miesięcznie)'], '⚠️', 'czerwiec: 87 dni > 45')
        self.assertIn('ponad 2× progu', rows['EIA ceny dzienne (publikowane co tydzień)'][4]); self.assertNotIn('godziny robocze', rows['EIA ceny dzienne (publikowane co tydzień)'][4])
        files['instytucje'] = {'tga': {'asof': '2026-09-18'}}
        self.assertIn('godziny robocze', {r[0]: r for r in k.swiezosc(files, now)}['TGA (Fiscal Data, dziennie)'][4], 'dzienne: próg w godzinach roboczych')
        self.assertEqual(len(rows), 12)

    def test_kapitalizacja_mediana(self):
        k = self.k
        rows = {'2026-09-%02d' % d: {'cap': 4.0 + (d % 3) * 0.1} for d in range(1, 11)}
        st, med, n, opis = k.kapitalizacja(rows, 4.5, '2026-09-26')
        self.assertEqual((st, n), ('✅', 10)); self.assertAlmostEqual(med, 4.1)
        self.assertEqual(k.kapitalizacja(rows, 6.5, '2026-09-26')[0], '⚠️'); self.assertEqual(k.kapitalizacja(rows, 9.2, '2026-09-26')[0], '❌'); self.assertEqual(k.kapitalizacja(rows, -1.5, '2026-09-26')[0], '❌')
        self.assertEqual(k.kapitalizacja({'2026-09-01': {'cap': 4.0}}, 9.0, '2026-09-26')[0], 'ℹ️', 'za mało historii — bez koloru')
        self.assertEqual(k.kapitalizacja(dict(rows, **{'2026-09-26': {'cap': 99.0}}), 4.5, '2026-09-26')[1], 4.1, 'dzisiejszy wpis nie wchodzi do mediany')
        self.assertEqual(k.kapitalizacja(rows, None, '2026-09-26')[0], '?')
        big = {'2026-%02d-%02d' % (1 + i // 28, 1 + i % 28): {'cap': float(i)} for i in range(60)}
        self.assertEqual(k.kapitalizacja(big, 45.0, '2026-09-26')[1], statistics.median(range(30, 60)), 'mediana z ostatnich 30 dni')
        # TGA: ta sama mediana, próg 1 pkt proc., bez czerwonego; dni bez odczytu TGA (pusta komórka) nie wchodzą do historii
        tg = {'2026-09-%02d' % d: {'cap': 4.0, 'tga': 3.0 + (d % 2) * 0.2} for d in range(1, 11)}; tg['2026-09-11'] = {'cap': 4.0}
        self.assertEqual(k.mediana_ocena(tg, 'tga', 3.5, '2026-09-26', k.TGA_PROG, None)[0], '✅'); self.assertEqual(k.mediana_ocena(tg, 'tga', 4.3, '2026-09-26', k.TGA_PROG, None)[0], '⚠️')
        self.assertEqual(k.mediana_ocena(tg, 'tga', 9.9, '2026-09-26', k.TGA_PROG, None)[0], '⚠️', 'TGA nigdy nie jest czerwone'); self.assertEqual(k.mediana_ocena(tg, 'tga', 3.5, '2026-09-26', 1.0, None)[2], 10)
        p = os.path.join(self.tmp, 'zgodnosc.csv'); k.zgodnosc_zapisz(p, {'2026-09-25': {'cap': 4.361, 'tga': 3.05}, '2026-09-26': {'cap': 4.4}})
        self.assertEqual(open(p, encoding='utf-8').read(), 'date,cap_gap_pct,tga_gap_pct\n2026-09-25,4.361,3.050\n2026-09-26,4.400,\n'); self.assertEqual(k.zgodnosc_csv(p), {'2026-09-25': {'cap': 4.361, 'tga': 3.05}, '2026-09-26': {'cap': 4.4}})

    def test_tga_i_wieloryby(self):
        k = self.k
        inst = {'tga': {'d': [['2026-09-22', 957409], ['2026-09-23', 947317], ['2026-09-24', 924627]]}}
        fred = {'series': {'WTREGEN': {'d': [['2026-09-16', 877028.0], ['2026-09-23', 977084.0]]}}}
        d, a, b, r = k.tga_porownanie(inst, fred)
        self.assertEqual((d, a, b), ('2026-09-23', 947317.0, 977084.0)); self.assertAlmostEqual(r, abs(947317 - 977084) / 977084 * 100)
        self.assertIsNone(k.tga_porownanie({}, fred)); self.assertIsNone(k.tga_porownanie(inst, {'series': {'WTREGEN': {'d': [['2026-01-01', 1]]}}}))
        arch = os.path.join(self.tmp, 'archiwum'); os.makedirs(arch, exist_ok=True); p = os.path.join(arch, 'wieloryby.csv')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('date,exchange,asset,balance,balance_usd,inflow_24h,outflow_24h,net_24h,block\n'
                    '2026-09-26,Binance,USDT,20000000000,20000000000,0,0,0,1\n2026-09-26,Binance,ETH,1000000,7000000000,0,0,0,1\n2026-09-26,OKX,USDC,1000000000,1000000000,0,0,0,1\n'
                    '2026-09-26,KuCoin,USDT,500000000,500000000,0,0,0,1\n2026-09-26,Bybit,ETH,1000,2900000,0,0,0,1\n'
                    '2026-09-27,Binance,USDT,20050000000,20050000000,60000000,10000000,50000000,2\n2026-09-27,Binance,ETH,1000000,7300000000,0,0,0,2\n'
                    '2026-09-27,OKX,USDC,1000400000,1000400000,0,0,0,2\n2026-09-27,KuCoin,USDT,600000000,600000000,0,0,0,2\n2026-09-27,Bybit,ETH,1100,3300000,0,0,0,2\n'
                    '2026-09-27,Bitfinex,USDT,1,1,0,0,0,2\n2026-09-27,Bitfinex,USDC,5,5,,,,2\n')
        d, pdn, zle, n = k.wieloryby_porownanie(p)
        self.assertEqual((d, pdn, n), ('2026-09-27', '2026-09-26', 5), 'Bitfinex bez poprzedniego dnia (i z pustymi przepływami) nie liczony')
        self.assertEqual([(g, a) for g, a, *_ in zle], [('KuCoin', 'USDT')],
                         'Binance USDT: zmiana 50 mln = netto 50 mln; Binance ETH: te same jednostki, wyższy kurs = nie przelew (v117); OKX 0,4 mln < 1 mln; Bybit ETH 100 × 3000 = 0,3 mln < 1 mln; KuCoin +100 mln vs 0')
        self.assertIsNone(k.wieloryby_porownanie(os.path.join(arch, 'nie-ma.csv')))
        with open(p, 'w', encoding='utf-8') as f:
            f.write('date,exchange,asset,balance,balance_usd,inflow_24h,outflow_24h,net_24h,block\n2026-09-26,Binance,USDT,100,1,0,0,0,1\n')
        self.assertIsNone(k.wieloryby_porownanie(p), 'jeden dzień = bez porównania')

    def test_etf_porownanie(self):
        k = self.k
        ceny = {'q': {'SPY': {'d': [['2026-09-24', 767.18, 1], ['2026-09-25', 771.35, 2]]}, 'EWJ': {'d': [['2026-09-25', 97.93, 1]]}, 'TUR': {'d': [['2026-09-25', 40.0, 1]]}, 'ILF': {'d': [['2026-09-25', None, 1]]}}}
        ix = {'etf': {'q': {'SPY': [['2026-09-25', 771.35]], 'EWJ': [['2026-09-24', 97.0], ['2026-09-25', 99.0]], 'ILF': [['2026-09-25', 30.0]], 'AGG': [['2026-09-25', 100.0]]}}}
        e = k.etf_porownanie(ceny, ix)
        self.assertEqual([(s, d, round(r, 3)) for s, d, a, b, r in e], [('SPY', '2026-09-25', 0.0), ('EWJ', '2026-09-25', round(abs(97.93 - 99.0) / 99.0 * 100, 3))], 'TUR bez drugiego źródła i ILF bez liczby pominięte; AGG spoza mapy')
        self.assertIsNone(k.etf_porownanie({}, ix)); self.assertIsNone(k.etf_porownanie(ceny, {}))
        R = {'zgodnosc': {'etf': {'porownane': 2, 'roznice': [{'symbol': 'EWJ', 'data': '2026-09-25', 'a': 97.93, 'b': 99.0, 'roznica_pct': 1.081}]}}, 'at': '2026-09-26T06:20:00+00:00', 'wynik': 'UWAGA', 'strona': {}, 'meta': {}, 'pliki': {}, 'actions': {}, 'swiezosc': [], 'uwagi': ['x'], 'bledy': []}
        self.assertIn('- ETF mapy (dwa źródła, ta sama data): porównane 2 symboli, różnice > 1%: 1 ⚠️ — EWJ.', k.raport_md(R))
        self.assertIn('ceny', k.PLIKI); self.assertIn('indeksy', k.PLIKI)

    def test_historia_i_raport(self):
        k = self.k
        p = os.path.join(self.tmp, 'historia.json')
        for i in range(1, 5):
            h = k.historia(p, {'at': f'2026-09-2{i}T06:20:00+00:00', 'bledy_zbieracza': 1 if i > 1 else 0, 'uwagi': 0, 'bledy': 0})
        self.assertEqual([x['bledy_zbieracza'] for x in h], [0, 1, 1, 1]); self.assertTrue(all(x > 0 for x in [y['bledy_zbieracza'] for y in h[-3:]]))
        h = k.historia(p, {'at': '2026-09-24T06:20:00+00:00', 'bledy_zbieracza': 0, 'uwagi': 0, 'bledy': 0})
        self.assertEqual(len(h), 4, 'ten sam czas = nadpisanie, nie duplikat'); self.assertEqual(h[-1]['bledy_zbieracza'], 0)
        for i in range(40):
            h = k.historia(p, {'at': f'2026-10-{1 + i // 28:02d}T{i % 24:02d}:00:00+00:00', 'bledy_zbieracza': 0, 'uwagi': 0, 'bledy': 0})
        self.assertEqual(len(h), k.HIST_N)
        E = lambda at, b: {'at': at, 'bledy_zbieracza': b}
        self.assertTrue(k.czerwone_z_historii([E('2026-09-24T06:20:00+00:00', 1), E('2026-09-25T06:20:00+00:00', 2), E('2026-09-26T06:20:00+00:00', 1)]))
        self.assertFalse(k.czerwone_z_historii([E('2026-09-26T06:20:00+00:00', 1), E('2026-09-26T09:00:00+00:00', 1), E('2026-09-26T12:00:00+00:00', 1)]), 'trzy przebiegi jednego dnia (po pushu) to nie 3 dni')
        self.assertTrue(k.czerwone_z_historii([E('2026-09-24T06:20:00+00:00', 1), E('2026-09-25T06:20:00+00:00', 0), E('2026-09-25T12:00:00+00:00', 1), E('2026-09-26T06:20:00+00:00', 1)]), 'liczy się ostatni przebieg dnia')
        self.assertFalse(k.czerwone_z_historii([E('2026-09-25T06:20:00+00:00', 1), E('2026-09-26T06:20:00+00:00', 1)]), 'dwa dni to za mało')
        R = {'at': '2026-09-26T06:20:00+00:00', 'wynik': 'UWAGA', 'strona': {'ok': True, 'http': 200, 'ms': 500}, 'meta': {'at': '2026-09-26T06:03:00+00:00', 'wiek_min': 17, 'zrodla': 55, 'bez_odpowiedzi': [], 'errors': [], 'notes': []},
             'pliki': {'etf': {'wiek_min': 34}, 'robots.txt': {'http': 200}}, 'actions': {'przebiegi_24h': 70, 'wg_wyniku': {'success': 70}},
             'swiezosc': [{'zrodlo': 'TIC (miesięcznie)', 'status': '⚠️', 'wiek_min': 80 * 1440, 'data': '2026-06', 'uwaga': 'próg 75 d 0 h'}],
             'zgodnosc': {'kapitalizacja': {'status': '✅', 'dzis_pct': 4.37, 'mediana_pct': 4.2, 'dni': 12, 'opis': 'odchylenie 0,17'}, 'ceny': {'bitcoin': {'a': 84154.0, 'b': 84174.9, 'roznica_pct': 0.025}},
                          'tga': {'data': '2026-09-23', 'fiscal_mln': 947317.0, 'fred_mln': 977084.0, 'roznica_pct': 3.05, 'status': 'ℹ️', 'mediana_pct': None, 'dni': 0, 'opis': 'historia 0 z 7 dni — bez oceny'}, 'wieloryby': None},
             'uwagi': ['TIC (miesięcznie): dane z 2026-06'], 'bledy': []}
        md = k.raport_md(R)
        self.assertRegex(md.splitlines()[0], r'^# Kontrola strony — \d{2}\.\d{2}\.\d{4}, \d{2}:\d{2} \(czas polski\)$', 'nagłówek w stałym formacie (czyta go zadanie w chmurze)')
        self.assertEqual(md.splitlines()[2], '**Wynik: UWAGA**'); self.assertTrue(md.splitlines()[4].startswith('⚠️ Uwag: 1'))
        self.assertIn('| Źródło | Status | Wiek danych | Data danych | Uwaga |', md); self.assertIn('| TIC (miesięcznie) | ⚠️ | 80 d 0 h | 2026-06 | próg 75 d 0 h |', md)
        self.assertIn('różnica dziś 4.37%, norma (mediana 12 dni) 4.20% — ✅', md); self.assertIn('TGA 2026-09-23: Fiscal Data 947,317 vs FRED 977,084 mln USD — różnica 3.05%, norma (mediana 0 dni) — — ℹ️ historia 0 z 7 dni — bez oceny.', md)
        self.assertIn('Cena BTC: 84,154 vs 84,175 USD — różnica 0.03% ✅', md); self.assertIn('Wieloryby: archiwum ma mniej niż dwa dni', md)
        ok = k.raport_md(dict(R, wynik='OK', uwagi=[], swiezosc=[], zgodnosc={}))
        self.assertEqual(ok.splitlines()[2], '**Wynik: OK**'); self.assertEqual(ok.splitlines()[4], '✅ Wszystko w normie.')
        bl = k.raport_md(dict(R, wynik='BŁĄD', bledy=['x']))
        self.assertEqual(bl.splitlines()[2], '**Wynik: BŁĄD**'); self.assertTrue(bl.splitlines()[4].startswith('❌ Błędów: 1'))

    def test_straz_kluczy(self):
        s = self.s
        root = os.path.join(self.tmp, 'straz'); os.makedirs(os.path.join(root, '_site', 'data'), exist_ok=True); os.makedirs(os.path.join(root, 'data'), exist_ok=True)
        with open(os.path.join(root, '_site', 'index.html'), 'w', encoding='utf-8') as f:
            f.write('<html>abc TAJNY-KLUCZ-12345 def TAJNY-KLUCZ-12345</html>')
        with open(os.path.join(root, 'data', 'x.json'), 'w', encoding='utf-8') as f:
            f.write('{"k":"DRUGI-KLUCZ-98765"}')
        sek, krotkie = s.sekrety({'FRED_KEY': 'TAJNY-KLUCZ-12345', 'EIA_KEY': 'abc', 'BLS_KEY': '', 'ETHERSCAN_KEY': ' DRUGI-KLUCZ-98765 ', 'INNE': 'X' * 20})
        self.assertEqual((sorted(sek), krotkie), (['ETHERSCAN_KEY', 'FRED_KEY'], ['EIA_KEY']), 'tylko znane nazwy; krótkie pominięte; białe znaki obcięte')
        tr, n = s.skanuj(sek, root)
        self.assertEqual(n, 2); self.assertEqual(sorted((a, b) for a, b, _ in tr), [('ETHERSCAN_KEY', 'data/x.json'), ('FRED_KEY', '_site/index.html'), ('FRED_KEY', '_site/index.html')])
        self.assertTrue(all('TAJNY' not in str(x) and 'DRUGI' not in str(x) for x in tr), 'wynik nie zawiera wartości')
        tr2, _ = s.skanuj({'FRED_KEY': b'NIEMA-TEGO-NIGDZIE'}, root); self.assertEqual(tr2, [])
        self.assertEqual(s.skanuj(sek, os.path.join(self.tmp, 'pusto'))[1], 0)

    def test_workflow_v115(self):
        wf = open(os.path.join(self.ROOT, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        i = wf.index('python3 narzedzia/straz_kluczy.py'); j = wf.index('actions/upload-pages-artifact')
        self.assertLess(i, j, 'straż kluczy przed publikacją'); self.assertGreater(i, wf.index('touch _site/.nojekyll'), 'straż po złożeniu strony')
        blok = wf[wf.rindex('- name:', 0, i):i]
        for k in ('ETHERSCAN_KEY', 'FRED_KEY', 'SOSOVALUE_KEY', 'MASSIVE_KEY', 'CENSUS_KEY'):
            self.assertIn(k + ':', blok, 'straż dostaje ten sam zestaw sekretów co zbieracz')
        self.assertNotIn('toJSON(secrets)', wf)
        ky = open(os.path.join(self.ROOT, '.github', 'workflows', 'kontrola.yml'), encoding='utf-8').read()
        self.assertIn('kontrola/zgodnosc.csv kontrola/historia.json', ky)
        src = open(os.path.join(self.ROOT, 'narzedzia', 'kontrola.py'), encoding='utf-8').read()
        self.assertIn("f'# Kontrola strony — {czas_pl(R[\"at\"])} (czas polski)'", src); self.assertIn("f'**Wynik: {R[\"wynik\"]}**'", src)


class IndeksyFmpV119(unittest.TestCase):
    """v119: FTSE 100 z FMP (plan bezpłatny EODHD nie daje indeksów LSE) — parser, jedno zapytanie po sesji, dopełnienie, przerwa po 402, wpis w części ix."""
    NOW = datetime.datetime(2026, 9, 26, 18, 0, tzinfo=datetime.timezone.utc)

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear()

    def test_parse(self):
        j = [{'symbol': '^FTSE', 'date': '2026-09-25', 'price': 8300.5, 'volume': 1}, {'date': '2026-09-24', 'close': 8250.0}, {'date': 'x', 'price': 1}, {'date': '2026-09-23', 'price': None}, 'x']
        self.assertEqual(zd.fmp_eod_parse(j), [['2026-09-24', 8250.0], ['2026-09-25', 8300.5]])
        with self.assertRaises(zd.IxPusto):
            zd.fmp_eod_parse([{'date': '2026-09-25', 'price': None}])
        with self.assertRaises(RuntimeError):
            zd.fmp_eod_parse({'Error Message': 'Limit'})

    def test_fetch_once_after_session_and_backoff(self):
        calls = []
        def gj(url, headers=None, timeout=30):
            calls.append(url); self.assertIn('symbol=%5EFTSE', url); self.assertIn('from=', url)
            return [{'date': '2026-09-25', 'price': 8300.5}, {'date': '2026-09-24', 'price': 8250.0}]
        part = {}
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            self.assertEqual(zd.ix_fmp('K', part, self.NOW, []), 1)
        self.assertEqual(part['FTSE']['d'], [['2026-09-24', 8250.0], ['2026-09-25', 8300.5]]); self.assertEqual((part['FTSE']['cc'], part['FTSE']['src']), ('gb', 'fmp'))
        self.assertIn('from=2025-09-21', calls[0], 'pierwsze pobranie: rok wstecz (IX_HIST_DAYS)')
        part['FTSE']['at'] = self.NOW.isoformat()
        with mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez zapytania')):
            self.assertEqual(zd.ix_fmp('K', part, self.NOW + datetime.timedelta(hours=2), []), 0, 'pobrane po dzisiejszej sesji — bez drugiego zapytania')
        nxt = self.NOW + datetime.timedelta(days=3, hours=1)   # poniedziałek 19:00 — sesja piątkowa i poniedziałkowa
        calls.clear()
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            self.assertEqual(zd.ix_fmp('K', part, nxt, []), 1)
        self.assertIn('from=2026-09-15', calls[0], 'dopełnienie: od ostatniej sesji minus zakładka 10 dni')
        errs = []
        with mock.patch.object(zd, 'get_json', side_effect=zd.urllib.error.HTTPError('u', 402, 'Payment', {}, None)):
            self.assertEqual(zd.ix_fmp('K', {}, self.NOW, errs), 0)
        self.assertEqual(errs, ['FMP HTTP 402 — FTSE (przerwa)'])
        bad = {'FTSE': {'bad_at': self.NOW.isoformat(), 'bad_n': 1, 'bad': 402}}
        with mock.patch.object(zd, 'get_json', side_effect=AssertionError('przerwa')):
            self.assertEqual(zd.ix_fmp('K', bad, self.NOW + datetime.timedelta(hours=5), []), 0, 'doba przerwy po 402')

    def test_build_adds_ftse_to_ix_part(self):
        def gj(url, headers=None, timeout=30):
            if 'financialmodelingprep' in url:
                return [{'date': '2026-09-25', 'price': 8300.5}]
            raise AssertionError('tylko FMP w tym teście')
        prev = {'at': '2026-09-26T16:00:00+00:00', 'ok': {'ix': True}, 'part_at': {'ix': '2026-09-26T16:00:00+00:00'}, 'ix_calls': {'d': '2026-09-26', 'n': 20},
                'ix': {'GSPC': {'cc': 'us', 'at': '2026-09-26T16:00:00+00:00', 'd': [['2026-09-25', 5.0]]}, 'FTMIB': {'bad': 404}}}
        with mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd, 'NOW', self.NOW.isoformat()):
            o = zd.build_indeksy({'FMP_KEY': 'K'}, prev, now=self.NOW)
        self.assertEqual(o['ix']['FTSE']['d'], [['2026-09-25', 8300.5]]); self.assertEqual(o['ix']['GSPC']['d'], [['2026-09-25', 5.0]], 'serie EODHD z poprzedniego pliku zostają')
        self.assertEqual(o['part_at']['ix'], self.NOW.isoformat()); self.assertIn('FTMIB', o['ix'], 'bez klucza EODHD część ix nie jest przebudowywana — stary wpis zostaje do następnego przebiegu EODHD')
        self.assertEqual(zd.META['errors'], [])
        with mock.patch.object(zd, 'get_json', side_effect=zd.urllib.error.HTTPError('u', 402, 'Payment', {}, None)), mock.patch.object(zd, 'NOW', self.NOW.isoformat()):
            o2 = zd.build_indeksy({'FMP_KEY': 'K'}, prev, now=self.NOW)
        self.assertEqual(o2['ix']['FTSE']['bad'], 402); self.assertNotIn('d', o2['ix']['FTSE']); self.assertTrue(zd.META['errors'] and 'FMP HTTP 402' in zd.META['errors'][0])
        self.assertIn('FMP_KEY', zd.IX_ACTIVE)


import io as _io_v121
import zipfile as _zip_v121


class CenyKryptoV121(unittest.TestCase):
    """v121 (obszar krypto-ceny): dzienne zamknięcia 10 par USDT z publicznych plików giełdy — parser CSV (ms / µs, nagłówek, złe wiersze,
    zamknięcie ≤ 0 = brak), dopełnienie plikami miesięcznymi + dziennymi, 404 = brak (nigdy zero), dzisiejsza doba poza plikiem, wczorajszy
    plik jeszcze nieopublikowany = bez uwagi, luka starsza = uwaga, miesięczny 404 = pliki dzienne, budżet czasu = ciąg dalszy, para z błędem
    = poprzednie dane + wpis, obcięcie do 420 dni, `only`, przepływ główny (pełna budowa co 60 min od full_at, ponowienie tylko złych par),
    przekroczenie czasu na końcu budżetu = ciąg dalszy, uporczywy błąd pliku = luka po 24 h (gdy dalszy plik się pobiera), kontrola codzienna."""
    TODAY = datetime.date(2026, 9, 26)
    # nagrane wiersze z prawdziwych plików (BTCUSDT-1d-2026-09-25.csv — mikrosekundy; BTCUSDT-1d-2024-12.csv — milisekundy)
    ROW_US = '1790294400000000,84410.24000000,85255.00000000,83183.00000000,84099.99000000,18056.26519000,1790380799999999,1520256978.69349630,2848596,8541.13394000,719546363.00050410,0'
    ROW_MS = '1733011200000,96407.99000000,97836.00000000,95693.88000000,97185.18000000,16938.60452000,1733097599999,1641327626.60622060,3342200,8114.89569000,786489040.19363780,0'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear()

    @staticmethod
    def _csv(days, base=100.0):
        lines = []
        for d in days:
            t0 = int(datetime.datetime.combine(d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp()) * 1_000_000
            c = base + (d - datetime.date(2025, 1, 1)).days * 0.5   # cena rośnie o 0,5 dziennie — zmiany dają się policzyć
            lines.append(f'{t0},{c - 0.5:.8f},{c + 1:.8f},{c - 1:.8f},{c:.8f},1000.00000000,{t0 + 86_399_999_999},{c * 1000:.8f},10,500.00000000,{c * 500:.8f},0')
        return '\n'.join(lines) + '\n'

    @staticmethod
    def _zip(name, text):
        buf = _io_v121.BytesIO()
        with _zip_v121.ZipFile(buf, 'w', _zip_v121.ZIP_DEFLATED) as z:
            z.writestr(name + '.csv', text)
        return buf.getvalue()

    def _server(self, missing=(), broken=(), pairs=None):
        """Udawane archiwum giełdy: plik miesięczny dla każdego zakończonego miesiąca (poza `missing`), plik dzienny dla każdego dnia przed
        dzisiejszym (poza `missing`); `broken` = HTTP 500. Zapisuje adresy zapytań."""
        calls = []
        today = self.TODAY

        def get_bytes(url, headers=None, timeout=60):
            calls.append(url)
            m = zd.re.search(r'/(\w+)-1d-(\d{4}-\d{2}(?:-\d{2})?)\.zip$', url)
            self.assertIsNotNone(m, url); pair, key = m.group(1), m.group(2)
            self.assertLessEqual(timeout, zd.KC_TIMEOUT); self.assertGreaterEqual(timeout, 1)
            if pairs is not None:
                self.assertIn(pair, pairs, 'zapytanie o parę spoza ponowienia')
            if key in broken or pair in broken:
                raise zd.urllib.error.HTTPError(url, 500, 'Server Error', {}, None)
            if key in missing:
                raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
            if len(key) == 7:   # miesięczny: tylko zakończone miesiące
                y, mo = int(key[:4]), int(key[5:])
                first = datetime.date(y, mo, 1); nxt = datetime.date(y + (mo == 12), mo % 12 + 1, 1)
                if nxt > today:
                    raise zd.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
                days = [first + datetime.timedelta(days=i) for i in range((nxt - first).days)]
            else:
                d = datetime.date.fromisoformat(key)
                self.assertLess(d, today, 'dzisiejsza doba nigdy nie jest pobierana')
                days = [d]
            return self._zip(f'{pair}-1d-{key}', self._csv(days))
        return get_bytes, calls

    def test_parse_rows_ms_us_header_and_bad_rows(self):
        text = 'open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore\n' + self.ROW_US + '\n' + self.ROW_MS + '\n' \
               + '1790380800000000,1,2,0.5,0.00000000,1,1790467199999999,5,1,1,1,0\n' + 'x,y\n' + '1790467200000000,1,2,0.5,abc,1,1790553599999999,5,1,1,1,0\n\n'
        out = zd.kc_parse(text)
        self.assertEqual(out, {'2026-09-25': (84099.99, 1520256979), '2024-12-01': (97185.18, 1641327627)}, 'µs i ms → dzień UTC; nagłówek, zero i śmieci pominięte')
        self.assertEqual(zd.kc_parse(''), {})
        self.assertEqual(zd.kc_parse('1790294400000000,1,2,0.5,3.5\n'), {'2026-09-25': (3.5, None)}, 'krótki wiersz: bez obrotu = None, nie zero')
        self.assertIsNone(zd.kc_num('0')); self.assertIsNone(zd.kc_num('nan')); self.assertIsNone(zd.kc_num(True)); self.assertEqual(zd.kc_num('2.5'), 2.5)

    def test_start_and_prev_rows(self):
        self.assertEqual(zd.kc_start(None, self.TODAY), datetime.date(2025, 7, 1), '14 pełnych miesięcy wstecz')
        self.assertEqual(zd.kc_start('2026-09-20', self.TODAY), datetime.date(2026, 9, 21))
        self.assertEqual(zd.kc_start(None, datetime.date(2026, 1, 15)), datetime.date(2024, 11, 1))
        rows = zd.kc_prev_rows({'d': [['2026-09-01', 5.0], ['zła', 1.0], ['2026-09-02', 0], ['2026-09-03', 7.5]], 'vol': [['2026-09-01', 100], ['2026-09-03', None]]})
        self.assertEqual(rows, {'2026-09-01': (5.0, 100), '2026-09-03': (7.5, None)})
        self.assertEqual(zd.kc_prev_rows(None), {}); self.assertEqual(zd.kc_prev_rows({'d': 'x'}), {})

    def test_first_fetch_monthly_then_daily_and_trim(self):
        gb, calls = self._server(missing=('2026-09-25',))   # wczorajszy plik jeszcze nieopublikowany (przed 2:00 UTC)
        with mock.patch.object(zd, 'get_bytes', gb):
            rec, notes, cut, err = zd.kc_pair('BTC', None, self.TODAY, zd.time.monotonic() + 60)
        mon = [u for u in calls if zd.re.search(r'/monthly/', u)]; day = [u for u in calls if zd.re.search(r'/daily/', u)]
        self.assertEqual(len(mon), 14, 'lipiec 2025 – sierpień 2026 miesięcznie'); self.assertTrue(mon[0].endswith('BTCUSDT-1d-2025-07.zip') and mon[-1].endswith('BTCUSDT-1d-2026-08.zip'))
        self.assertEqual(len(day), 25, 'wrzesień dziennie do wczoraj'); self.assertTrue(day[0].endswith('2026-09-01.zip') and day[-1].endswith('2026-09-25.zip'))
        self.assertFalse(cut); self.assertIsNone(err); self.assertEqual(notes, [], 'brak wczorajszego pliku to nie uwaga')
        self.assertEqual(len(rec['d']), zd.KC_KEEP, 'obcięte do 420 dni'); self.assertEqual(rec['d'][-1][0], '2026-09-24'); self.assertEqual(rec['d'][0][0], '2025-08-01')
        self.assertEqual(len(rec['vol']), zd.KC_KEEP); self.assertEqual(rec['vol'][-1][0], '2026-09-24')
        self.assertTrue(all(isinstance(r[1], float) and r[1] > 0 for r in rec['d'])); self.assertTrue(all(isinstance(r[1], int) for r in rec['vol']))
        self.assertFalse(any(u.endswith('2026-09-26.zip') or u.endswith('2026-09.zip') for u in calls), 'ani dzisiejszy dzień, ani bieżący miesiąc')

    def test_steady_state_only_days_after_last_close(self):
        gb, calls = self._server(missing=('2026-09-25',))
        prev = {'d': [['2026-09-22', 400.0], ['2026-09-23', 400.5]], 'vol': [['2026-09-23', 7]]}
        with mock.patch.object(zd, 'get_bytes', gb):
            rec, notes, cut, err = zd.kc_pair('ETH', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertEqual([u[-14:] for u in calls], ['2026-09-24.zip', '2026-09-25.zip'], 'tylko dni po ostatnim zamknięciu')
        self.assertEqual([r[0] for r in rec['d']], ['2026-09-22', '2026-09-23', '2026-09-24']); self.assertEqual(rec['d'][1][1], 400.5, 'stare wiersze bez zmian')
        self.assertEqual(rec['vol'][0], ['2026-09-23', 7]); self.assertEqual(notes, []); self.assertFalse(cut); self.assertIsNone(err)
        gb2, calls2 = self._server()   # godzinę później: wczorajszy plik już jest
        with mock.patch.object(zd, 'get_bytes', gb2):
            rec2, notes2, _, _ = zd.kc_pair('ETH', rec, self.TODAY, zd.time.monotonic() + 60)
        self.assertEqual([u[-14:] for u in calls2], ['2026-09-25.zip']); self.assertEqual(rec2['d'][-1][0], '2026-09-25')
        gb3, calls3 = self._server()   # nic nowego: wczoraj już w pliku → żadnego zapytania
        with mock.patch.object(zd, 'get_bytes', gb3):
            rec3, _, _, _ = zd.kc_pair('ETH', rec2, self.TODAY, zd.time.monotonic() + 60)
        self.assertEqual(calls3, []); self.assertEqual(rec3['d'], rec2['d'])

    def test_gap_is_missing_not_zero_and_noted(self):
        gb, calls = self._server(missing=('2026-09-22',))
        prev = {'d': [['2026-09-20', 400.0]]}
        with mock.patch.object(zd, 'get_bytes', gb):
            rec, notes, cut, err = zd.kc_pair('SOL', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertEqual([r[0] for r in rec['d']], ['2026-09-20', '2026-09-21', '2026-09-23', '2026-09-24', '2026-09-25'], 'dzień bez pliku = brak wiersza, nie zero')
        self.assertEqual(notes, ['2026-09-22: brak pliku']); self.assertIsNone(err)
        self.assertFalse(any(r[1] == 0 for r in rec['d']))

    def test_monthly_404_or_error_falls_back_to_daily_files(self):
        for srv, note in ((dict(missing=('2026-08',)), None), (dict(broken=('2026-08',)), '2026-08: plik miesięczny z błędem (HTTP Error 500')):
            gb, calls = self._server(**srv)   # miesięczny za sierpień jeszcze nieopublikowany (404) albo uszkodzony (500)
            prev = {'d': [['2026-08-25', 300.0]]}
            with mock.patch.object(zd, 'get_bytes', gb):
                rec, notes, cut, err = zd.kc_pair('XRP', prev, self.TODAY, zd.time.monotonic() + 60)
            self.assertTrue(calls[0].endswith('XRPUSDT-1d-2026-08.zip')); self.assertEqual(sum('/daily/' in u for u in calls), 6 + 25, 'sierpień 26–31 i wrzesień 1–25 dziennie')
            self.assertEqual(rec['d'][0], ['2026-08-25', 300.0]); self.assertEqual(rec['d'][-1][0], '2026-09-25'); self.assertEqual(len(rec['d']), 32); self.assertIsNone(err); self.assertFalse(cut)
            if note is None:
                self.assertEqual(notes, [])
            else:
                self.assertEqual(len(notes), 1); self.assertTrue(notes[0].startswith(note), notes)

    def test_http_error_keeps_progress_and_budget_cuts(self):
        gb, calls = self._server(broken=('2026-09-24',))
        prev = {'d': [['2026-09-20', 400.0]]}
        with mock.patch.object(zd, 'get_bytes', gb):
            rec, notes, cut, err = zd.kc_pair('ADA', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertFalse(cut); self.assertEqual(err[0], '2026-09-24'); self.assertIsInstance(err[1], zd.urllib.error.HTTPError)
        self.assertEqual([r[0] for r in rec['d']], ['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23'], 'wiersze pobrane przed błędem zostają')
        self.assertFalse(any(u.endswith('2026-09-25.zip') for u in calls), 'po błędzie para staje (następny przebieg zacznie od dnia z błędem)')
        gb2, calls2 = self._server()
        with mock.patch.object(zd, 'get_bytes', gb2):
            rec, notes, cut, err = zd.kc_pair('ADA', prev, self.TODAY, zd.time.monotonic() - 1)   # budżet już wyczerpany
        self.assertTrue(cut); self.assertIsNone(err); self.assertEqual(calls2, []); self.assertEqual(rec['d'], [['2026-09-20', 400.0]], 'bez zapytań, dane bez zmian')
        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: b'nie zip'):
            rec, notes, cut, err = zd.kc_pair('ADA', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertEqual(err[0], '2026-09-21'); self.assertIsInstance(err[1], zd.zipfile.BadZipFile); self.assertEqual(rec['d'], [['2026-09-20', 400.0]])
        with mock.patch.object(zd, 'get_bytes', lambda url, headers=None, timeout=60: self._zip('x', 'a').replace(b'x.csv', b'x.txt')):
            rec, notes, cut, err = zd.kc_pair('ADA', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertIsInstance(err[1], ValueError)

    def test_timeout_near_budget_end_is_cut_not_error(self):
        """Przegląd v121: ostatnie zapytanie przed końcem budżetu dostaje krótszy limit czasu; jego przekroczenie to koniec budżetu
        (ciąg dalszy), nie błąd pary — wiersze pobrane wcześniej zostają. Przekroczenie pełnego KC_TIMEOUT pozostaje błędem."""
        gb, _ = self._server()

        def slow(url, headers=None, timeout=60):   # odpowiedź na 24.09 wolniejsza niż reszta budżetu
            if url.endswith('2026-09-24.zip') and timeout < zd.KC_TIMEOUT:
                raise TimeoutError('The read operation timed out')
            return gb(url, headers, timeout)

        def slow_connect(url, headers=None, timeout=60):   # przekroczenie czasu połączenia — urllib opakowuje je w URLError
            if timeout < zd.KC_TIMEOUT:
                raise zd.urllib.error.URLError(TimeoutError('timed out'))
            return gb(url, headers, timeout)

        def always_timeout(url, headers=None, timeout=60):
            raise TimeoutError('The read operation timed out')

        prev = {'d': [['2026-09-20', 400.0]]}
        with mock.patch.object(zd, 'get_bytes', slow):
            rec, notes, cut, err = zd.kc_pair('BTC', prev, self.TODAY, zd.time.monotonic() + 5)
        self.assertTrue(cut); self.assertIsNone(err); self.assertEqual(rec['d'][-1][0], '2026-09-23', 'wiersze sprzed końca budżetu zostają')
        with mock.patch.object(zd, 'get_bytes', slow_connect):
            rec, notes, cut, err = zd.kc_pair('BTC', prev, self.TODAY, zd.time.monotonic() + 5)
        self.assertTrue(cut); self.assertIsNone(err); self.assertEqual(rec['d'], [['2026-09-20', 400.0]])
        with mock.patch.object(zd, 'get_bytes', always_timeout):
            rec, notes, cut, err = zd.kc_pair('BTC', prev, self.TODAY, zd.time.monotonic() + 60)
        self.assertFalse(cut); self.assertEqual(err[0], '2026-09-21'); self.assertIsInstance(err[1], TimeoutError, 'pełny limit przekroczony = błąd')
        with mock.patch.object(zd, 'get_bytes', slow), mock.patch.object(zd, 'KC_BUDGET', 5):
            o = zd.build_ceny_krypto({'q': {'BTC': prev}}, only={'BTC'}, today=self.TODAY)
        self.assertEqual(o['bledy'], {}); self.assertEqual(o['blok'], {}); self.assertEqual(zd.META['errors'], []); self.assertIs(o['ok']['BTC'], False)
        self.assertEqual(o['q']['BTC']['d'][-1][0], '2026-09-23'); self.assertTrue(any('budżet czasu' in n and n.startswith('Ceny krypto: BTC') for n in zd.META['notes']), zd.META['notes'])

    def test_persistent_bad_file_becomes_gap_after_24h(self):
        """Przegląd v121: plik dzienny, który zawodzi stale, nie może zatrzymać pary na zawsze — po KC_SKIP_H h jego dzień zostaje luką
        (brak, nigdy zero), ale tylko gdy późniejszy plik tej pary się pobierze."""
        prev = {'at': _iso(30), 'q': {'ETH': {'d': [['2026-09-22', 400.0], ['2026-09-23', 400.5]]}}}
        gb, calls = self._server(broken=('2026-09-24',), pairs=('ETHUSDT',))
        with mock.patch.object(zd, 'get_bytes', gb):
            o1 = zd.build_ceny_krypto(prev, only={'ETH'}, today=self.TODAY)
        self.assertEqual(o1['blok'], {'ETH': {'d': '2026-09-24', 'od': zd.NOW}}); self.assertTrue(o1['bledy']['ETH'].startswith('ETH: 2026-09-24: HTTP Error 500'), o1['bledy'])
        self.assertEqual(o1['q']['ETH']['d'][-1][0], '2026-09-23'); self.assertIs(o1['ok']['ETH'], False)
        young = dict(o1, blok={'ETH': {'d': '2026-09-24', 'od': _iso(23 * 60)}})   # błąd od 23 h — jeszcze bez luki
        with mock.patch.object(zd, 'get_bytes', self._server(broken=('2026-09-24',))[0]):
            o2 = zd.build_ceny_krypto(young, only={'ETH'}, today=self.TODAY)
        self.assertEqual(o2['blok'], young['blok'], 'ten sam dzień — początek błędu przechodzi dalej'); self.assertIn('ETH', o2['bledy'])
        old = dict(o1, blok={'ETH': {'d': '2026-09-24', 'od': _iso(25 * 60)}})   # błąd od 25 h, a 25.09 już jest
        zd.META['errors'].clear(); zd.META['notes'].clear()
        with mock.patch.object(zd, 'get_bytes', self._server(broken=('2026-09-24',))[0]):
            o3 = zd.build_ceny_krypto(old, only={'ETH'}, today=self.TODAY)
        self.assertEqual([r[0] for r in o3['q']['ETH']['d']], ['2026-09-22', '2026-09-23', '2026-09-25'], 'dzień z uporczywym błędem = luka, nie zero')
        self.assertIs(o3['ok']['ETH'], True); self.assertEqual(o3['bledy'], {}); self.assertEqual(o3['blok'], {}); self.assertEqual(zd.META['errors'], [])
        self.assertTrue(any(n.startswith('Ceny krypto: ETH 2026-09-24: plik z błędem dłużej niż 24 h') and 'dzień pominięty' in n for n in zd.META['notes']), zd.META['notes'])
        with mock.patch.object(zd, 'get_bytes', self._server(broken=('2026-09-24', '2026-09-25'))[0]):   # dzień później też błąd — bez luki
            o4 = zd.build_ceny_krypto(old, only={'ETH'}, today=self.TODAY)
        self.assertEqual(o4['blok'], old['blok']); self.assertTrue(o4['bledy']['ETH'].startswith('ETH: 2026-09-24: '), o4['bledy']); self.assertEqual(o4['q']['ETH']['d'][-1][0], '2026-09-23')
        self.assertEqual(zd.kc_age_h('zły'), -1); self.assertEqual(zd.kc_age_h(None), -1); self.assertEqual(zd.kc_age_h('2026-09-26T10:00:00'), -1, 'bez strefy — nigdy „dość stary”')

    def test_build_isolates_pairs_and_reports(self):
        gb, calls = self._server(broken=('ETHUSDT',))
        prev = {'at': '2026-09-26T10:00:00+00:00', 'ok': {'ETH': True}, 'part_at': {'ETH': '2026-09-26T09:00:00+00:00'},
                'q': {'ETH': {'d': [['2026-09-24', 300.0]], 'vol': [['2026-09-24', 5]]}}}
        with mock.patch.object(zd, 'get_bytes', gb):
            o = zd.build_ceny_krypto(prev, today=self.TODAY)
        self.assertEqual(sorted(o['q']), sorted(zd.TR_CR_SYMS), 'wszystkie pary w pliku'); self.assertEqual(o['at'], zd.NOW); self.assertEqual(o['quote'], 'USDT'); self.assertEqual(o['keep'], 420)
        self.assertEqual(o['full_at'], zd.NOW, 'pełna budowa ustawia full_at')
        self.assertIs(o['ok']['BTC'], True); self.assertIs(o['ok']['ETH'], False); self.assertEqual(o['part_at']['BTC'], zd.NOW)
        self.assertEqual(o['q']['ETH'], prev['q']['ETH'], 'para z błędem: poprzednie dane'); self.assertEqual(o['part_at']['ETH'], '2026-09-26T09:00:00+00:00', '… z własnym czasem')
        self.assertTrue(o['bledy']['ETH'].startswith('ETH: 2026-09-25: HTTP Error 500'), o['bledy']); self.assertEqual(list(o['bledy']), ['ETH'])
        self.assertEqual(o['blok'], {'ETH': {'d': '2026-09-25', 'od': zd.NOW}})
        self.assertEqual(len(zd.META['errors']), 1); self.assertTrue(zd.META['errors'][0].startswith('Ceny krypto: ETH: 2026-09-25: HTTP Error 500'), zd.META['errors'])
        self.assertEqual(o['q']['BTC']['d'][-1][0], '2026-09-25'); self.assertEqual(len(o['q']['BTC']['d']), zd.KC_KEEP)
        self.assertNotIn('Binance', o['src'], 'opis źródła w pliku bez nazwy dostawcy (nazwa tylko w komentarzu kodu i na stronie Źródła)'); self.assertIn('Publiczne dane rynkowe giełdy', o['src'])
        # ponowienie tylko złej pary: reszta przepisana z poprzedniego pliku z jej ok / part_at; full_at bez zmian
        gb2, calls2 = self._server(pairs=('ETHUSDT',))
        zd.META['errors'].clear()
        o_prev = dict(o, full_at='2026-09-26T11:40:00+00:00')
        with mock.patch.object(zd, 'get_bytes', gb2):
            o2 = zd.build_ceny_krypto(o_prev, only={'ETH'}, today=self.TODAY)
        self.assertTrue(all('ETHUSDT' in u for u in calls2) and calls2, 'tylko ETH pobierany')
        self.assertIs(o2['ok']['ETH'], True); self.assertEqual(o2['q']['ETH']['d'][-1][0], '2026-09-25'); self.assertEqual(o2['bledy'], {}); self.assertEqual(o2['blok'], {})
        self.assertIs(o2['q']['BTC'], o['q']['BTC']); self.assertIs(o2['ok']['BTC'], True); self.assertEqual(o2['part_at']['BTC'], o['part_at']['BTC']); self.assertEqual(zd.META['errors'], [])
        self.assertEqual(o2['full_at'], '2026-09-26T11:40:00+00:00', 'ponowienie części nie przesuwa zegara pełnej budowy'); self.assertEqual(o2['at'], zd.NOW)
        self.assertEqual(zd.build_ceny_krypto({'at': '2026-09-26T11:00:00+00:00', 'q': o['q']}, only=set(), today=self.TODAY)['full_at'], '2026-09-26T11:00:00+00:00', 'stary plik bez full_at — od `at`')

    def test_build_budget_note_and_no_data(self):
        gb, calls = self._server()
        with mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, 'KC_BUDGET', -1):
            o = zd.build_ceny_krypto({'q': {'BTC': {'d': [['2026-09-20', 1.0]]}}}, today=self.TODAY)
        self.assertEqual(calls, []); self.assertIs(o['ok']['BTC'], False); self.assertNotIn('ETH', o['q']); self.assertEqual(o['bledy'], {}); self.assertEqual(zd.META['errors'], [])
        self.assertTrue(any('budżet czasu' in n and n.startswith('Ceny krypto: BTC') for n in zd.META['notes']), zd.META['notes'])
        with mock.patch.object(zd, 'get_bytes', side_effect=RuntimeError('sieć')), self.assertRaises(RuntimeError) as cm:
            zd.build_ceny_krypto(None, today=self.TODAY)
        self.assertIn('żadna para nie odpowiedziała', str(cm.exception)); self.assertTrue(zd.META['errors'][0].startswith('Ceny krypto: BTC: 2025-07-01: sieć; ETH: 2025-07-01: sieć'), zd.META['errors'])

    def _stubs(self):
        names = [n for n in dir(zd) if n.startswith('build_') and n != 'build_ceny_krypto' and callable(getattr(zd, n))]
        return [mock.patch.object(zd, n, side_effect=RuntimeError('offline')) for n in names]

    ENV_EMPTY = ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY', 'SITE_URL', 'CACHE_DIR')

    def _main(self, prev, build=None, extra=()):
        """main() bez sieci: pozostałe budowniczowie udają awarię; `build` zastępuje build_ceny_krypto (None = prawdziwy); `extra` = dodatkowe łatki."""
        saved, stubs = {}, self._stubs()
        env = {k: '' for k in self.ENV_EMPTY}
        pats = [mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)),
                mock.patch.object(zd, 'previous', lambda name: prev if name == 'ceny-krypto' else None)] + list(extra)
        if build is not None:
            pats.append(mock.patch.object(zd, 'build_ceny_krypto', side_effect=build))
        [p.start() for p in stubs + pats]
        try:
            zd.main()
        finally:
            [p.stop() for p in reversed(stubs + pats)]
        return saved

    def test_main_wiring(self):
        src = open(zd.__file__, encoding='utf-8').read()
        self.assertIn("prev_kc = previous('ceny-krypto')", src); self.assertIn("META['errors'].append(mask(f'{KC_LABEL}: {e}')); META['ok']['ceny-krypto'] = False", src)
        self.assertIn("if prev_kc: save('ceny-krypto', prev_kc)", src); self.assertEqual(zd.KC_EVERY, 60); self.assertEqual(zd.KC_LABEL, 'Ceny krypto')
        self.assertIn("kc_young = isinstance(prev_kc, dict) and fresh({'at': prev_kc.get('full_at') or prev_kc.get('at')}, KC_EVERY)", src, 'wiek od pełnej budowy (wzór v104)')
        ok_all = {s: True for s in zd.TR_CR_SYMS}
        young = {'at': _iso(10), 'ok': ok_all, 'q': {'BTC': {'d': [['2026-09-25', 1.0]]}}}
        saved = self._main(young, AssertionError('młody plik — nie wolno pobierać'))
        self.assertIs(saved['ceny-krypto'], young); self.assertEqual(zd.META['ok']['ceny-krypto'], 'cached')
        calls = []
        built = {'at': zd.NOW, 'ok': ok_all, 'bledy': {}, 'q': {}}
        young_bad = dict(young, ok=dict(ok_all, ETH=False), full_at=_iso(30))
        saved = self._main(young_bad, lambda p, only=None: calls.append((p, only)) or built)
        self.assertEqual(calls, [(young_bad, {'ETH'})], 'młody plik ze złą parą — dobierana tylko ona'); self.assertIs(saved['ceny-krypto'], built); self.assertIs(zd.META['ok']['ceny-krypto'], True)
        calls.clear()
        retried = dict(young_bad, at=_iso(10), full_at=_iso(70))   # `at` świeże po ponowieniu, pełna budowa sprzed 70 min
        saved = self._main(retried, lambda p, only=None: calls.append((p, only)) or built)
        self.assertEqual(calls, [(retried, None)], 'pełna budowa liczy się od full_at, nie od `at`')
        calls.clear()
        old = dict(young, at=_iso(120))
        saved = self._main(old, lambda p, only=None: calls.append((p, only)) or built)
        self.assertEqual(calls, [(old, None)], 'stary plik — wszystkie pary'); self.assertIs(saved['ceny-krypto'], built)
        saved = self._main(None, lambda p, only=None: dict(built, bledy={'ETH': 'ETH: x'}))
        self.assertIs(zd.META['ok']['ceny-krypto'], False, 'para z błędem = źródło bez pełnej odpowiedzi')
        saved = self._main(old, RuntimeError('offline'))
        self.assertIs(saved['ceny-krypto'], old, 'awaria = poprzedni plik'); self.assertIs(zd.META['ok']['ceny-krypto'], False); self.assertIn('Ceny krypto: offline', zd.META['errors'])
        # listy zaślepek w testach przepływu głównego (krotki nazw w pętlach „for f in (…)”) znają nowy budowniczy — liczone wyrażeniem,
        # nie szukane dosłownie (dosłowny tekst byłby też w tej linii i sprawdzenie zawsze by przechodziło)
        me = open(__file__, encoding='utf-8').read()
        tup = zd.re.compile(r"for (?:f|fn) in\s*\(\s*('build_\w+'(?:\s*,\s*'build_\w+')*)\s*\)", zd.re.S)
        lists = [m.group(1) for m in tup.finditer(me) if "'build_wieloryby'" in m.group(1)]
        self.assertGreaterEqual(len(lists), 18, 'krotki zaślepek przepływu głównego')
        self.assertEqual([s[:60] for s in lists if "'build_ceny_krypto'" not in s], [], 'każda krotka zaślepek zawiera build_ceny_krypto')

    def test_main_one_broken_pair_does_not_freeze_others(self):
        """Przegląd v121: para z uporczywym błędem (tu ETH, HTTP 500) nie zamraża pozostałych. Ponowienie co 20 min dotyczy tylko jej
        i odświeża `at`, ale pełna budowa co 60 min liczy się od full_at — następnego dnia pozostałe 9 par dostaje nowe zamknięcie."""
        def run(prev, today):
            self.TODAY = today
            gb, calls = self._server(broken=('ETHUSDT',))
            noon = datetime.datetime.combine(today, datetime.time(12), tzinfo=datetime.timezone.utc)
            saved = self._main(prev, extra=(mock.patch.object(zd, 'get_bytes', gb), mock.patch.object(zd, '_now_utc', lambda: noon)))
            return saved['ceny-krypto'], sorted({zd.re.search(r'/klines/(\w+)/', u).group(1) for u in calls})

        o1, p1 = run(None, datetime.date(2026, 9, 26))
        self.assertEqual(p1, sorted(s + 'USDT' for s in zd.TR_CR_SYMS), 'pierwszy przebieg: wszystkie pary')
        self.assertEqual(o1['q']['BTC']['d'][-1][0], '2026-09-25'); self.assertIs(o1['ok']['ETH'], False); self.assertNotIn('ETH', o1['q']); self.assertEqual(o1['full_at'], zd.NOW)
        o1 = dict(o1, at=_iso(20), full_at=_iso(20))   # 20 min później: plik młody, ETH z błędem — ponawiana tylko ETH
        o2, p2 = run(o1, datetime.date(2026, 9, 26))
        self.assertEqual(p2, ['ETHUSDT']); self.assertEqual(o2['full_at'], o1['full_at'], 'ponowienie nie przesuwa zegara pełnej budowy'); self.assertEqual(o2['at'], zd.NOW)
        self.assertIs(o2['q']['BTC'], o1['q']['BTC'])
        o2 = dict(o2, at=_iso(20), full_at=_iso(70))   # następnego dnia: 20 min po ponowieniu, 70 min po pełnej budowie
        o3, p3 = run(o2, datetime.date(2026, 9, 27))
        self.assertEqual(len(p3), 10, 'pełna budowa: wszystkie pary')
        for s in zd.TR_CR_SYMS:
            if s != 'ETH':
                self.assertEqual(o3['q'][s]['d'][-1][0], '2026-09-26', s + ': nowe zamknięcie mimo uporczywego błędu ETH'); self.assertIs(o3['ok'][s], True, s)
        self.assertIs(o3['ok']['ETH'], False); self.assertEqual(o3['full_at'], zd.NOW); self.assertIs(zd.META['ok']['ceny-krypto'], False)

    def test_kontrola_and_page(self):
        root = os.path.dirname(os.path.abspath(__file__))
        k = open(os.path.join(root, 'narzedzia', 'kontrola.py'), encoding='utf-8').read()
        pliki = zd.re.search(r'^PLIKI = \[(.*?)\]$', k, zd.re.M).group(1); self.assertIn("'ceny-krypto'", pliki)
        self.assertIn("'ceny-krypto': 180", k)
        html = open(os.path.join(root, 'index.html'), encoding='utf-8').read()
        self.assertIn('<section class="panel pcard" id="c-ceny-krypto" hidden></section>', html); self.assertIn("srvJSON('ceny-krypto')", html)
        self.assertIn("const KC_SYMS=['BTC','ETH','XRP','BNB','SOL','DOGE','ADA','TRX','LINK','AVAX'];", html); self.assertEqual(list(zd.TR_CR_SYMS), ['BTC', 'ETH', 'XRP', 'BNB', 'SOL', 'DOGE', 'ADA', 'TRX', 'LINK', 'AVAX'])


import io
import re


class InsiderV121(unittest.TestCase):
    """v121: insiderzy spółek USA (Form 4 z EDGAR, bez klucza; kontakt User-Agent z sekretu SEC_CONTACT): dzień gotowy od 04:00 UTC dnia
    następnego, indeks o stałej szerokości kolumn i każde zgłoszenie raz (numer, nie ścieżka), dokument zgłoszenia (P/S × cena; brak ceny =
    pominięte), kolejka na kilka przebiegów, limit 700, 403 „brak pliku” (weekend) ≠ 403 blokada, ostatni dzień bez indeksu sprawdzany znowu,
    błędy sieci (zgłoszenie nie znika po jednym błędzie, dzień nie utyka), brak ≠ zero, uzupełnianie wstecz, okablowanie main(), kontrola, straż i strona."""
    UTC = datetime.timezone.utc
    C = 'kontakt-testowy@example.org'   # atrapa — prawdziwy adres tylko w sekrecie SEC_CONTACT
    SAT = datetime.datetime(2026, 9, 26, 4, 20, tzinfo=datetime.timezone.utc)   # sobota 04:20 UTC: piątek 25.09 gotowy
    F1, F2, F3 = 'edgar/data/1555279/0001535264-26-000053.txt', 'edgar/data/1800/0001306119-26-000009.txt', 'edgar/data/935036/0002080452-26-000013.txt'
    F1_OWNER = 'edgar/data/1535264/0001535264-26-000053.txt'   # to samo zgłoszenie pod CIK osoby zgłaszającej
    # odpowiedź magazynu na brak pliku (dzień bez indeksu) i strona blokady (nagranie 26.09.2026, skrócone)
    S3_DENIED = b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code><Message>Access Denied</Message><RequestId>X</RequestId></Error>'
    BLOCKED = (b'<!DOCTYPE html>\n<html><head><title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title></head><body>'
               b'<h1>Your Request Originates from an Undeclared Automated Tool</h1><p>Please declare your traffic by updating your user agent to include company specific information.</p></body></html>')
    IDX = ('Description:           Daily Index of EDGAR Dissemination Feed by Form Type\nLast Data Received:    Sep 25, 2026\nComments:              webmaster@sec.gov\n \n \n'
           'Form Type   Company Name                                                  CIK\n      Date Filed  File Name\n' + '-' * 141 + '\n'
           '1-A POS          Modern Mining Technology Corp.                                1898722     20260925    edgar/data/1898722/0001213900-26-103507.txt                                                \n'
           '4                908 Devices Inc.                                              1555279     20260925    edgar/data/1555279/0001535264-26-000053.txt                                                \n'
           '4                ABBOTT LABORATORIES                                           1800        20260925    edgar/data/1800/0001306119-26-000009.txt                                                \n'
           '4                ACI WORLDWIDE, INC.                                           935036      20260925    edgar/data/935036/0002080452-26-000013.txt                                                \n'
           '4                AWM Investment Company, Inc.                                  1535264     20260925    edgar/data/1535264/0001535264-26-000053.txt                                                \n'
           '4                908 Devices Inc.                                              1555279     20260925    edgar/data/1555279/0001535264-26-000053.txt                                                \n'
           '4/A              ACME CORP                                                     1           20260925    edgar/data/1/0000000001-26-000001.txt                                                \n'
           '424B2            BANK                                                          2           20260925    edgar/data/2/0000000002-26-000001.txt                                                \n')
    # nagranie 26.09.2026 (skrócone: bez adresu i podpisu): zgłoszenie 0001535264-26-000053 — dwie sprzedaże na rynku, spółka 908 Devices (MASS)
    REC = ('<SEC-DOCUMENT>0001535264-26-000053.txt : 20260925\n<SEC-HEADER>0001535264-26-000053.hdr.sgml : 20260925\n<ACCEPTANCE-DATETIME>20260925161925\n'
           'ACCESSION NUMBER:\t\t0001535264-26-000053\nCONFORMED SUBMISSION TYPE:\t4\n</SEC-HEADER>\n<DOCUMENT>\n<TYPE>4\n<SEQUENCE>1\n<FILENAME>primary_doc.xml\n<TEXT>\n<XML>\n'
           '<?xml version="1.0"?>\n<ownershipDocument>\n    <schemaVersion>X0609</schemaVersion>\n    <documentType>4</documentType>\n    <periodOfReport>2026-09-23</periodOfReport>\n'
           '    <issuer>\n        <issuerCik>0001555279</issuerCik>\n        <issuerName>908 Devices Inc.</issuerName>\n        <issuerTradingSymbol>MASS</issuerTradingSymbol>\n    </issuer>\n'
           '    <reportingOwner>\n        <reportingOwnerId>\n            <rptOwnerCik>0001535264</rptOwnerCik>\n            <rptOwnerName>AWM Investment Company, Inc.</rptOwnerName>\n        </reportingOwnerId>\n'
           '        <reportingOwnerRelationship>\n            <isDirector>0</isDirector>\n            <isOfficer>0</isOfficer>\n            <isTenPercentOwner>1</isTenPercentOwner>\n            <isOther>0</isOther>\n        </reportingOwnerRelationship>\n    </reportingOwner>\n'
           '    <aff10b5One>0</aff10b5One>\n    <nonDerivativeTable>\n'
           '        <nonDerivativeTransaction>\n            <securityTitle>\n                <value>Common Stock</value>\n            </securityTitle>\n            <transactionDate>\n                <value>2026-09-23</value>\n            </transactionDate>\n'
           '            <transactionCoding>\n                <transactionFormType>4</transactionFormType>\n                <transactionCode>S</transactionCode>\n                <equitySwapInvolved>0</equitySwapInvolved>\n            </transactionCoding>\n'
           '            <transactionTimeliness>\n                <value></value>\n            </transactionTimeliness>\n'
           '            <transactionAmounts>\n                <transactionShares>\n                    <value>1000</value>\n                </transactionShares>\n                <transactionPricePerShare>\n                    <value>11.25</value>\n                    <footnoteId id="F1"/>\n                </transactionPricePerShare>\n'
           '                <transactionAcquiredDisposedCode>\n                    <value>D</value>\n                </transactionAcquiredDisposedCode>\n            </transactionAmounts>\n'
           '            <postTransactionAmounts>\n                <sharesOwnedFollowingTransaction>\n                    <value>4755773</value>\n                </sharesOwnedFollowingTransaction>\n            </postTransactionAmounts>\n'
           '            <ownershipNature>\n                <directOrIndirectOwnership>\n                    <value>I</value>\n                </directOrIndirectOwnership>\n                <natureOfOwnership>\n                    <value>By Limited Partnershp</value>\n                </natureOfOwnership>\n            </ownershipNature>\n        </nonDerivativeTransaction>\n'
           '        <nonDerivativeTransaction>\n            <securityTitle>\n                <value>Common Stock</value>\n            </securityTitle>\n            <transactionDate>\n                <value>2026-09-24</value>\n            </transactionDate>\n'
           '            <transactionCoding>\n                <transactionFormType>4</transactionFormType>\n                <transactionCode>S</transactionCode>\n                <equitySwapInvolved>0</equitySwapInvolved>\n            </transactionCoding>\n'
           '            <transactionAmounts>\n                <transactionShares>\n                    <value>24000</value>\n                    <footnoteId id="F2"/>\n                    <footnoteId id="F3"/>\n                </transactionShares>\n'
           '                <transactionPricePerShare>\n                    <value>11.3594</value>\n                    <footnoteId id="F1"/>\n                </transactionPricePerShare>\n                <transactionAcquiredDisposedCode>\n                    <value>D</value>\n                </transactionAcquiredDisposedCode>\n            </transactionAmounts>\n'
           '            <postTransactionAmounts>\n                <sharesOwnedFollowingTransaction>\n                    <value>4754773</value>\n                </sharesOwnedFollowingTransaction>\n            </postTransactionAmounts>\n'
           '            <ownershipNature>\n                <directOrIndirectOwnership>\n                    <value>I</value>\n                </directOrIndirectOwnership>\n            </ownershipNature>\n        </nonDerivativeTransaction>\n    </nonDerivativeTable>\n'
           '    <footnotes>\n        <footnote id="F1">This is a weighted average price.</footnote>\n    </footnotes>\n</ownershipDocument>\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n')
    REC_SELL = 1000 * 11.25 + 24000 * 11.3594

    def setUp(self):
        zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear()
        self.p_sleep = mock.patch.object(zd.time, 'sleep', lambda s: None); self.p_sleep.start()
        zd._INS_LAST[0] = 0.0

    def tearDown(self):
        self.p_sleep.stop()

    @staticmethod
    def _doc(issuer, ticker, trs):
        """Dokument .txt z jednym blokiem XML; trs = [(kod, A/D, akcje, cena), …] (cena None = wiersz bez ceny)."""
        rows = ''.join(f'<nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle><transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{c}</transactionCode></transactionCoding>'
                       f'<transactionAmounts><transactionShares><value>{sh}</value></transactionShares>'
                       + (f'<transactionPricePerShare><value>{px}</value></transactionPricePerShare>' if px is not None else '<transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>')
                       + f'<transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>' for c, ad, sh, px in trs)
        return ('<SEC-DOCUMENT>x.txt : 20260925\n<SEC-HEADER>x\n</SEC-HEADER>\n<DOCUMENT>\n<TYPE>4\n<TEXT>\n<XML>\n<?xml version="1.0"?>\n<ownershipDocument><schemaVersion>X0609</schemaVersion><documentType>4</documentType>'
                f'<issuer><issuerCik>1</issuerCik><issuerName>{issuer}</issuerName><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>'
                f'<nonDerivativeTable>{rows}</nonDerivativeTable></ownershipDocument>\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n')

    def _docs(self):
        return {self.F1: self.REC, self.F2: self._doc('ABBOTT LABORATORIES', 'ABT', [('P', 'A', 1000, 100.0), ('F', 'D', 5, 100.0)]),
                self.F3: self._doc('ACI WORLDWIDE, INC.', 'ACIW', [('P', 'A', 10, 50.0), ('S', 'D', 20, 50.0)])}

    @staticmethod
    def _rd(path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _http(self, url, code, body=b''):
        return zd.urllib.error.HTTPError(url, code, 'err', {}, io.BytesIO(body))

    def _get(self, calls, docs, idx, fail=None):
        """Udaje EDGAR: idx = {YYYYMMDD: tekst indeksu} (inne dni → 403 z treścią magazynu „AccessDenied”, jak w weekend), docs = {plik: tekst},
        fail = {plik: wyjątek albo funkcja(url) → wyjątek}; sprawdza User-Agent (nazwa programu i kontakt z sekretu)."""
        def get(url, headers=None, timeout=30):
            calls.append(url); self.assertEqual((headers or {}).get('User-Agent'), zd.ins_ua(self.C), 'każde zapytanie z nazwą i kontaktem')
            if 'daily-index' in url:
                d = url.rsplit('form.', 1)[1][:8]
                if d not in idx:
                    raise self._http(url, 403, self.S3_DENIED)
                return 200, idx[d]
            f = url.split('/Archives/')[1]
            if fail and f in fail:
                x = fail[f]
                raise x(url) if callable(x) and not isinstance(x, BaseException) else x
            return 200, docs[f]
        return get

    def _run(self, prev, now, calls, docs=None, idx=None, fail=None, budget=None):
        with mock.patch.object(zd, 'get', self._get(calls, docs or {}, idx or {}, fail)):
            return zd.build_insider(prev, now=now, budget=budget, contact=self.C)

    def test_target_day_and_urls(self):
        D = datetime.datetime
        self.assertEqual(zd.ins_target(self.SAT), datetime.date(2026, 9, 25), 'sobota 04:20 UTC: piątek gotowy')
        self.assertEqual(zd.ins_target(D(2026, 9, 26, 3, 40, tzinfo=self.UTC)), datetime.date(2026, 9, 24), 'przed 04:00 UTC piątek jeszcze niegotowy (Form 4 przyjmowane do 22:00 ET)')
        self.assertEqual(zd.ins_target(D(2026, 9, 25, 23, 20, tzinfo=self.UTC)), datetime.date(2026, 9, 24))
        self.assertEqual(zd.ins_idx_url(datetime.date(2026, 9, 25)), 'https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260925.idx')
        self.assertEqual(zd.ins_idx_url(datetime.date(2026, 1, 2)), 'https://www.sec.gov/Archives/edgar/daily-index/2026/QTR1/form.20260102.idx')
        self.assertEqual(zd.ins_idx_url(datetime.date(2026, 12, 31)), 'https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261231.idx')
        self.assertLessEqual(1 / zd.INS_TEMPO, 8.0); self.assertEqual(zd.INS_MAX, 700); self.assertEqual(zd.INS_BUDGET, 120)
        # kontakt tylko z sekretu: w kodzie sam wzór, adres e-mail wymagany (bez niego urząd blokuje), zły = część wyłączona
        self.assertNotIn('@', zd.INS_UA); self.assertEqual(zd.ins_ua(' ' + self.C + ' '), 'CapitalFlowAI/1.0 (' + self.C + ')')
        for bad in (None, '', '   ', 'https://capitalflowai-app.github.io/', 'a b@c.org', 'x@y', 'x@y.org)', 'a@b.org\r\nX: y', 'a' * 120 + '@b.org'):
            self.assertIsNone(zd.ins_ua(bad), repr(bad))
        with self.assertRaises(RuntimeError):
            zd.build_insider({}, now=self.SAT, contact='')

    def test_parse_index(self):
        rows = zd.ins_parse_index(self.IDX)
        self.assertEqual(rows, [['1555279', '908 Devices Inc.', self.F1], ['1800', 'ABBOTT LABORATORIES', self.F2], ['935036', 'ACI WORLDWIDE, INC.', self.F3]],
                         'tylko typ „4” (bez 4/A i 424B2); to samo zgłoszenie pod CIK spółki i osoby zgłaszającej — raz; stała szerokość kolumn')
        self.assertEqual(zd.ins_parse_index('śmieć\n4 x\n'), [])
        self.assertTrue(zd.ins_index_ok(self.IDX)); self.assertFalse(zd.ins_index_ok(self.BLOCKED.decode())); self.assertFalse(zd.ins_index_ok(''))
        # jak w nagraniu 25.09.2026: zgłoszenie 0001104659-26-110960 (Redwire) pod 8 numerami CIK = jedno zgłoszenie
        head = self.IDX.split('\n' + '-' * 141 + '\n')[0] + '\n' + '-' * 141 + '\n'
        many = head + ''.join(f'4                FILER {i}                                                       {1732533 + i:<11d} 20260925    edgar/data/{1732533 + i}/0001104659-26-110960.txt\n' for i in range(8))
        self.assertEqual(len(zd.ins_parse_index(many)), 1)

    def test_parse_doc_recorded(self):
        o = zd.ins_parse_doc(self.REC)
        self.assertEqual((o['issuer'], o['ticker'], o['n_buy'], o['n_sell'], o['buy']), ('908 Devices Inc.', 'MASS', 0, 2, 0.0))
        self.assertAlmostEqual(o['sell'], self.REC_SELL, places=2)

    def test_parse_doc_rules(self):
        o = zd.ins_parse_doc(self._doc('Acme Corp', 'acme', [('P', 'A', 100, 10.5), ('S', 'D', 50, 20), ('F', 'D', 10, 20), ('M', 'A', 10, 1), ('A', 'A', 10, 0), ('P', 'A', 5, None), ('S', 'D', '1,000', '2.5'), ('P', 'A', 'x', 3), ('P', 'D', 7, 3)]))
        self.assertEqual((o['issuer'], o['ticker'], o['n_buy'], o['n_sell']), ('Acme Corp', 'ACME', 1, 2))
        self.assertAlmostEqual(o['buy'], 1050.0); self.assertAlmostEqual(o['sell'], 1000.0 + 2500.0)   # F, M, A, bez ceny, cena 0, akcje „x”, P ze zbyciem — pominięte
        self.assertIsNone(zd.ins_parse_doc(self._doc('X', 'NONE', []))['ticker'])
        self.assertIsNone(zd.ins_parse_doc(self._doc('X', '', []))['ticker'])
        with self.assertRaises(ValueError):
            zd.ins_parse_doc('<SEC-DOCUMENT>\n<XML>\n<other/>\n</XML>\n')
        with self.assertRaises(ValueError):
            zd.ins_parse_doc('bez xml')
        with self.assertRaises(ValueError):
            zd.ins_parse_doc('<XML><ownershipDocument><issuer></XML>')

    def test_full_day_after_previous(self):
        prev = {'at': '2026-09-25T04:20:00+00:00', 'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': [['2026-09-24', 1.0, 2.0, 1, 1]], 'skipped': ['2026-09-20']}
        calls = []
        o = self._run(prev, self.SAT, calls, self._docs(), {'20260925': self.IDX})
        self.assertEqual(calls, [zd.ins_idx_url(datetime.date(2026, 9, 25))] + ['https://www.sec.gov/Archives/' + f for f in (self.F1, self.F2, self.F3)],
                         'indeks i trzy dokumenty — zgłoszenie wymienione pod dwoma CIK pobrane i policzone raz')
        self.assertEqual((o['day'], o['done'], o['ok'], o['checked']), ('2026-09-25', True, {'sec': True}, '2026-09-25'))
        self.assertEqual((o['n_filings'], o['n_queued'], o['n_parsed'], o['n_fail'], o['n_buy'], o['n_sell']), (3, 3, 3, 0, 2, 2))
        self.assertAlmostEqual(o['buys_usd'], 100500.0, places=2); self.assertAlmostEqual(o['sells_usd'], self.REC_SELL + 1000.0, places=2)
        self.assertEqual(o['ratio'], round(100500.0 / (self.REC_SELL + 1000.0), 3)); self.assertEqual(o['share'], round(100 * 100500.0 / (100500.0 + self.REC_SELL + 1000.0), 1))
        self.assertEqual(o['top_buys'], [['ABBOTT LABORATORIES', 'ABT', 100000.0], ['ACI WORLDWIDE, INC.', 'ACIW', 500.0]])
        self.assertEqual([r[:2] for r in o['top_sells']], [['908 Devices Inc.', 'MASS'], ['ACI WORLDWIDE, INC.', 'ACIW']]); self.assertAlmostEqual(o['top_sells'][0][2], self.REC_SELL, places=2)
        self.assertEqual(o['hist'], [['2026-09-24', 1.0, 2.0, 1, 1], ['2026-09-25', o['buys_usd'], o['sells_usd'], 2, 2]])
        for k in ('pending', 'iss', 'tries'):
            self.assertNotIn(k, o)
        self.assertEqual(o['skipped'], ['2026-09-20']); self.assertEqual(o['at'], zd.NOW); self.assertEqual(o['src'], zd.INS_SRC)
        self.assertEqual((zd.META['errors'], zd.META['notes']), ([], []))
        calls.clear()
        o2 = self._run(o, self.SAT + datetime.timedelta(minutes=20), calls)
        self.assertEqual(calls, [], 'ten sam dzień już odczytany — bez zapytań'); self.assertEqual(o2['hist'], o['hist']); self.assertEqual(o2['day'], '2026-09-25')

    def test_first_run_backfills_and_skips_days_without_index(self):
        calls = []
        o = self._run(None, self.SAT, calls, self._docs(), {'20260925': self.IDX})
        idx_calls = [u for u in calls if 'daily-index' in u]
        self.assertEqual(len(idx_calls), zd.INS_BACK_DAYS, 'pierwszy przebieg: dni wstecz od najstarszego'); self.assertTrue(idx_calls[0].endswith('form.20260827.idx')); self.assertTrue(idx_calls[-1].endswith('form.20260925.idx'))
        self.assertEqual(len(o['skipped']), zd.INS_BACK_DAYS - 1); self.assertNotIn('2026-09-25', o['skipped']); self.assertEqual(o['day'], '2026-09-25'); self.assertTrue(o['done'])
        self.assertEqual((o['checked'], zd.META['errors']), ('2026-09-25', []), 'brak indeksu (403 „AccessDenied”) to nie błąd')
        calls.clear()
        self._run(o, self.SAT + datetime.timedelta(hours=3), calls)
        self.assertEqual(calls, [], 'do następnego dnia bez zapytań')

    def test_partial_day_continues_next_run(self):
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': [['2026-09-24', 1.0, 2.0, 1, 1]]}
        calls = []
        o1 = self._run(prev, self.SAT, calls, self._docs(), {'20260925': self.IDX}, budget=-1)   # jedno zapytanie na przebieg: tylko indeks
        self.assertEqual(len(calls), 1); self.assertFalse(o1['done']); self.assertEqual(o1['pending'], [self.F1, self.F2, self.F3]); self.assertEqual(o1['n_parsed'], 0)
        self.assertIsNone(o1['buys_usd']); self.assertIsNone(o1['ratio']); self.assertEqual(o1['hist'][-1], ['2026-09-25', None, None, None, None], 'nic nie odczytane = brak, nie zero')
        self.assertEqual((o1['n_buy'], o1['n_sell']), (None, None), 'bez odczytanych zgłoszeń liczba zgłoszeń z zakupem/sprzedażą to brak, nie 0')
        self.assertTrue(any('w toku' in n for n in zd.META['notes'])); self.assertEqual(zd.META['errors'], [])
        calls.clear()
        o2 = self._run(o1, self.SAT + datetime.timedelta(minutes=20), calls, self._docs(), budget=-1)
        self.assertEqual(calls, ['https://www.sec.gov/Archives/' + self.F1], 'ciąg dalszy bez ponownego indeksu'); self.assertEqual(o2['pending'], [self.F2, self.F3]); self.assertEqual(o2['n_parsed'], 1)
        self.assertEqual(o2['buys_usd'], 0.0); self.assertAlmostEqual(o2['sells_usd'], self.REC_SELL, places=2); self.assertEqual(o2['hist'][-1][:3], ['2026-09-25', 0.0, round(self.REC_SELL, 2)])
        self.assertEqual((o2['n_buy'], o2['n_sell']), (0, 1), 'po odczycie: prawdziwe zero zakupów')
        self.assertEqual(o2['top_sells'][0][:2], ['908 Devices Inc.', 'MASS']); self.assertIn('iss', o2)
        calls.clear()
        o3 = self._run(o2, self.SAT + datetime.timedelta(minutes=40), calls, self._docs())
        self.assertEqual(calls, ['https://www.sec.gov/Archives/' + f for f in (self.F2, self.F3)]); self.assertTrue(o3['done']); self.assertNotIn('pending', o3); self.assertNotIn('iss', o3)
        self.assertEqual((o3['n_parsed'], o3['n_buy'], o3['n_sell']), (3, 2, 2)); self.assertAlmostEqual(o3['buys_usd'], 100500.0, places=2); self.assertEqual(len(o3['hist']), 2)

    def test_weekend_without_index_is_not_an_error(self):
        prev = {'day': '2026-09-25', 'done': True, 'checked': '2026-09-25', 'hist': [['2026-09-25', 5.0, 6.0, 1, 1]], 'buys_usd': 5.0, 'sells_usd': 6.0}
        sun = datetime.datetime(2026, 9, 27, 4, 20, tzinfo=self.UTC)
        calls = []
        o = self._run(prev, sun, calls)
        self.assertEqual(calls, [zd.ins_idx_url(datetime.date(2026, 9, 26))]); self.assertEqual(o['skipped'], [])
        self.assertEqual(o['checked'], '2026-09-25', 'ostatni gotowy dzień bez indeksu nie jest jeszcze zamknięty — indeks może się spóźnić')
        self.assertEqual((o['day'], o['hist'], o['buys_usd'], o['ok']), ('2026-09-25', prev['hist'], 5.0, {'sec': True})); self.assertEqual(zd.META['errors'], [])
        calls.clear()
        o = self._run(o, sun + datetime.timedelta(hours=5), calls)
        self.assertEqual(calls, [zd.ins_idx_url(datetime.date(2026, 9, 26))], 'ten sam dzień sprawdzony znowu (jedno zapytanie na przebieg)')
        mon = datetime.datetime(2026, 9, 28, 4, 20, tzinfo=self.UTC); calls.clear()
        o2 = self._run(o, mon, calls)
        self.assertEqual(calls, [zd.ins_idx_url(datetime.date(2026, 9, 26)), zd.ins_idx_url(datetime.date(2026, 9, 27))])
        self.assertEqual((o2['skipped'], o2['checked']), (['2026-09-26'], '2026-09-26'), 'sobota już nie jest ostatnim dniem — zapamiętana jako pusta')
        tue = datetime.datetime(2026, 9, 29, 4, 20, tzinfo=self.UTC); calls.clear()
        o3 = self._run(o2, tue, calls, self._docs(), {'20260928': self.IDX})
        self.assertEqual(calls[:2], [zd.ins_idx_url(datetime.date(2026, 9, 27)), zd.ins_idx_url(datetime.date(2026, 9, 28))]); self.assertEqual(o3['skipped'], ['2026-09-26', '2026-09-27'])
        self.assertEqual(o3['day'], '2026-09-28'); self.assertEqual([r[0] for r in o3['hist']], ['2026-09-25', '2026-09-28']); self.assertEqual(zd.META['errors'], [])

    def test_blocked_index_is_an_error_not_a_holiday(self):
        """403 ze stroną blokady (zły User-Agent, limit) albo 429 — błąd; dzień handlu nie trafia do „skipped”, `checked` stoi, następny przebieg go czyta."""
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': [['2026-09-24', 1.0, 2.0, 1, 1]]}
        for code, body, why in ((403, self.BLOCKED, 'User-Agent'), (403, b'<html>Forbidden</html>', 'odmowa'), (429, b'', 'limit')):
            zd.META['errors'].clear(); calls = []

            def blocked(url, headers=None, timeout=30, e=(code, body)):
                calls.append(url); raise self._http(url, *e)
            with mock.patch.object(zd, 'get', blocked):
                o = zd.build_insider(prev, now=self.SAT, contact=self.C)
            self.assertEqual(len(calls), 1, 'po blokadzie żadnych dalszych zapytań w tym przebiegu')
            self.assertEqual((o['checked'], o['skipped'], o['ok'], o['day'], o['hist']), ('2026-09-24', [], {'sec': False}, '2026-09-24', prev['hist']))
            self.assertTrue(zd.META['errors'] and zd.META['errors'][0].startswith('Insiderzy: indeks 2026-09-25: HTTP %d' % code) and why in zd.META['errors'][0], zd.META['errors'])
        calls = []
        o2 = self._run(o, self.SAT + datetime.timedelta(minutes=20), calls, self._docs(), {'20260925': self.IDX})
        self.assertEqual((o2['day'], o2['done'], o2['ok']), ('2026-09-25', True, {'sec': True}), 'dzień odczytany w następnym przebiegu')
        # pierwszy przebieg przy blokadzie: nic nie trafia do „skipped” (także dni robocze), wyjątek → main zostawia poprzedni plik
        zd.META['errors'].clear(); calls = []

        def blocked_all(url, headers=None, timeout=30):
            calls.append(url); raise self._http(url, 403, self.BLOCKED)
        with mock.patch.object(zd, 'get', blocked_all), self.assertRaises(RuntimeError):
            zd.build_insider(None, now=self.SAT, contact=self.C)
        self.assertEqual(len(calls), 1, 'pierwszy przebieg: po blokadzie koniec, żaden dzień nie trafia do „skipped”')
        # 200, ale bez nagłówka indeksu (strona błędu) — błąd, a nie „dzień bez zgłoszeń”
        zd.META['errors'].clear(); calls = []
        o3 = self._run(prev, self.SAT, calls, {}, {'20260925': self.BLOCKED.decode()})
        self.assertEqual((o3['checked'], o3['day'], o3['ok']), ('2026-09-24', '2026-09-24', {'sec': False})); self.assertIn('bez nagłówka indeksu', zd.META['errors'][0])

    def test_cap_700_filings(self):
        head = self.IDX.split('\n' + '-' * 141 + '\n')[0] + '\n' + '-' * 141 + '\n'
        idx = head + ''.join(f'4                SPÓŁKA {i:04d}                                                   {1000 + i:<11d} 20260925    edgar/data/{1000 + i}/{i:010d}-26-000001.txt\n' for i in range(702))
        self.assertEqual(len(zd.ins_parse_index(idx)), 702)
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': []}
        o = self._run(prev, self.SAT, [], {}, {'20260925': idx}, budget=-1)
        self.assertEqual((o['n_filings'], o['n_queued'], len(o['pending'])), (702, 700, 700)); self.assertTrue(any('policzono pierwsze 700' in n for n in o['notes']))
        self.assertTrue(any('policzono pierwsze 700' in n for n in zd.META['notes'])); self.assertEqual(zd.META['errors'], [])

    def test_network_errors_stop_run_and_keep_queue(self):
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': []}
        calls, t_out = [], zd.urllib.error.URLError('timed out')
        o = self._run(prev, self.SAT, calls, self._docs(), {'20260925': self.IDX}, fail={self.F1: t_out, self.F2: t_out, self.F3: t_out})
        self.assertEqual(len(calls), 1 + zd.INS_BLEDY, 'indeks i trzy błędy z rzędu — koniec na ten przebieg')
        self.assertEqual((o['ok'], o['done'], sorted(o['pending']), o['n_parsed'], o['n_fail']), ({'sec': False}, False, sorted([self.F1, self.F2, self.F3]), 0, 0), 'nic nie przepada')
        self.assertTrue(zd.META['errors'] and zd.META['errors'][0].startswith('Insiderzy: zgłoszenie 0002080452-26-000013.txt: ') and 'timed out' in zd.META['errors'][0], zd.META['errors'])
        # jedno zgłoszenie z błędem serwera: reszta odczytana, ono czeka na następny przebieg (jedna próba na przebieg, bez błędu części)
        zd.META['errors'].clear(); zd.META['notes'].clear(); calls.clear()
        f503 = {self.F1: lambda url: self._http(url, 503)}
        o2 = self._run(o, self.SAT + datetime.timedelta(minutes=20), calls, self._docs(), fail=f503)
        self.assertEqual(len([u for u in calls if u.endswith(self.F1)]), 1)
        self.assertEqual((o2['done'], o2['pending'], o2['n_parsed'], o2['n_fail'], o2['ok'], o2['tries']), (False, [self.F1], 2, 0, {'sec': True}, {'0001535264-26-000053.txt': 2}))
        self.assertAlmostEqual(o2['buys_usd'], 100500.0, places=2); self.assertEqual(zd.META['errors'], [])
        # INS_PROBY-ty przebieg z błędem: zgłoszenie pominięte (n_fail, notatka) — dzień domknięty, nie utyka
        calls.clear()
        o3 = self._run(o2, self.SAT + datetime.timedelta(minutes=40), calls, self._docs(), fail=f503)
        self.assertEqual((o3['done'], o3['n_parsed'], o3['n_fail'], o3['ok']), (True, 2, 1, {'sec': True})); self.assertNotIn('tries', o3); self.assertNotIn('pending', o3)
        self.assertTrue(any('pominięte po 3 przebiegach' in n and 'HTTP 503' in n for n in zd.META['notes'])); self.assertAlmostEqual(o3['hist'][-1][1], 100500.0, places=2)

    def test_document_missing_or_blocked(self):
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': []}
        calls = []
        # dokument, którego nie ma (403 „AccessDenied” albo 404) — pominięty i policzony w n_fail; reszta dnia odczytana, bez błędu
        o = self._run(prev, self.SAT, calls, self._docs(), {'20260925': self.IDX}, fail={self.F1: lambda url: self._http(url, 403, self.S3_DENIED), self.F2: lambda url: self._http(url, 404)})
        self.assertEqual((o['done'], o['n_parsed'], o['n_fail'], o['ok']), (True, 1, 2, {'sec': True})); self.assertEqual(zd.META['errors'], [])
        # blokada przy dokumencie — koniec przebiegu od razu, zgłoszenie zostaje na początku kolejki, bez liczenia prób
        calls.clear()
        o2 = self._run(prev, self.SAT, calls, self._docs(), {'20260925': self.IDX}, fail={self.F2: lambda url: self._http(url, 403, self.BLOCKED)})
        self.assertEqual(calls[-1], 'https://www.sec.gov/Archives/' + self.F2); self.assertEqual(len(calls), 3)
        self.assertEqual((o2['done'], o2['pending'], o2['n_parsed'], o2['n_fail'], o2['ok'], o2['tries']), (False, [self.F2, self.F3], 1, 0, {'sec': False}, {}))
        self.assertTrue(any('HTTP 403 (nieznany program' in e for e in zd.META['errors']), zd.META['errors'])

    def test_index_error_keeps_previous_state_or_raises(self):
        prev = {'day': '2026-09-24', 'done': True, 'checked': '2026-09-24', 'hist': [['2026-09-24', 1.0, 2.0, 1, 1]], 'buys_usd': 1.0}
        def boom(url, headers=None, timeout=30):
            raise zd.urllib.error.URLError('dns')
        with mock.patch.object(zd, 'get', boom):
            o = zd.build_insider(prev, now=self.SAT, contact=self.C)
        self.assertEqual((o['day'], o['hist'], o['ok']), ('2026-09-24', prev['hist'], {'sec': False})); self.assertEqual(o['checked'], '2026-09-24', 'nieudany dzień — do sprawdzenia ponownie')
        self.assertTrue(zd.META['errors'] and zd.META['errors'][0].startswith('Insiderzy: indeks 2026-09-25'))
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get', boom), self.assertRaises(RuntimeError):
            zd.build_insider(None, now=self.SAT, contact=self.C)
        def http500(url, headers=None, timeout=30):
            raise zd.urllib.error.HTTPError(url, 500, 'err', {}, None)
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get', http500):
            o3 = zd.build_insider(prev, now=self.SAT, contact=self.C)
        self.assertEqual(o3['ok'], {'sec': False}); self.assertIn('HTTP 500', zd.META['errors'][0]); self.assertEqual(o3['skipped'], [])

    def test_budget_late_run_and_hist_limit(self):
        with mock.patch.object(zd, '_RUN_T0', [zd.time.monotonic() - zd.INS_LATE - 1]), mock.patch.object(zd, 'get', self._get([], {}, {})):
            o = zd.build_insider({'day': '2026-09-25', 'done': True, 'checked': '2026-09-25'}, now=self.SAT, contact=self.C)
        self.assertEqual(o['day'], '2026-09-25')
        old = [[f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}', float(i), 0.0, 1, 0] for i in range(zd.INS_HIST + 10)]
        h = zd.ins_hist(old + ['x', ['zła', 1, 2, 3, 4]], ['2026-09-25', 9.0, 9.0, 1, 1])
        self.assertEqual(len(h), zd.INS_HIST); self.assertEqual(h[-1], ['2026-09-25', 9.0, 9.0, 1, 1]); self.assertEqual(h, sorted(h))
        self.assertEqual(zd.ins_top({'a': ['A', 'AA', 5.0, 0.0], 'b': ['B', None, 7.0, 3.0], 'c': ['C', 'CC', 0.0, 1.0], 'd': 'śmieć'}, 2), [['B', None, 7.0], ['A', 'AA', 5.0]])
        self.assertEqual(zd.ins_top({'b': ['B', None, 7.0, 3.0], 'c': ['C', 'CC', 0.0, 1.0]}, 3), [['B', None, 3.0], ['C', 'CC', 1.0]])

    def test_main_wiring(self):
        saved, seen = {}, {}; prev = {'at': _iso(30), 'day': '2026-09-24'}
        env0 = {'SOSOVALUE_KEY': '', 'COINGECKO_KEY': '', 'FINNHUB_KEY': '', 'TWELVEDATA_KEY': '', 'COINMARKETCAP_KEY': '', 'FRED_KEY': '', 'SITE_URL': '', 'CACHE_DIR': ''}
        stubs = [mock.patch.object(zd, n, side_effect=RuntimeError('offline')) for n in dir(zd) if n.startswith('build_') and n != 'build_insider']
        [s.start() for s in stubs]

        def fake(p, now=None, budget=None, contact=None):
            seen['contact'] = contact; seen['secret_masked'] = contact in zd.SECRETS
            return {'at': 'x', 'ok': {'sec': True}, 'prev': p}

        def run(env, build):
            zd.META['errors'].clear(); zd.META['notes'].clear(); zd.META['ok'].clear(); saved.clear(); seen.clear()
            with mock.patch.object(zd, 'save', lambda name, obj: saved.__setitem__(name, obj)), mock.patch.object(zd, 'previous', lambda name: prev if name == 'insider' else None), \
                 mock.patch.dict(os.environ, dict(env0, **env), clear=False), mock.patch.object(zd, 'build_insider', build):
                zd.main()
        try:
            run({'SEC_CONTACT': ' ' + self.C + ' '}, fake)
            self.assertIs(saved['insider']['prev'], prev); self.assertIs(zd.META['ok']['insider'], True)
            self.assertEqual(seen, {'contact': self.C, 'secret_masked': True}, 'kontakt z sekretu, maskowany w komunikatach')
            run({'SEC_CONTACT': self.C}, mock.Mock(side_effect=RuntimeError('padło ' + self.C)))
            self.assertIs(saved['insider'], prev, 'awaria = poprzedni plik'); self.assertIs(zd.META['ok']['insider'], False)
            self.assertIn('Insiderzy: padło ***', zd.META['errors'], 'adres kontaktowy nigdy w komunikacie błędu')
            for val, why in (('', 'brak SEC_CONTACT'), ('https://capitalflowai-app.github.io/', 'SEC_CONTACT to nie adres e-mail')):
                b = mock.Mock(side_effect=AssertionError('nie wolno wołać bez kontaktu'))
                run({'SEC_CONTACT': val}, b)
                self.assertFalse(b.called); self.assertIs(saved.get('insider'), prev, 'bez kontaktu: poprzedni plik zostaje, bez zapytań')
                self.assertNotIn('insider', zd.META['ok']); self.assertTrue(any(n.startswith(why) and 'insiderzy' in n for n in zd.META['notes']), zd.META['notes'])
                self.assertFalse(any('Insiderzy' in e for e in zd.META['errors']))
        finally:
            [s.stop() for s in stubs]
        src = self._rd(zd.__file__)
        self.assertIn("prev_ins = previous('insider')", src); self.assertIn("save('insider', ins)", src); self.assertIn("os.environ.get('SEC_CONTACT', '')", src)
        blk = src[src.index('# ===================== v121: INSIDERZY'):min(x for x in (src.find('\n# =====================', src.index('# ===================== v121: INSIDERZY') + 10), src.find('\n# --- v', src.index('# ===================== v121: INSIDERZY') + 10), src.index('\ndef main():')) if x > 0)]   # do następnego nagłówka bloku albo def main() (niezależnie od kolejności łatek)
        self.assertIn('informacja publiczna SEC', blk); self.assertIn('accessing-edgar-data', blk); self.assertNotIn('domena publiczna', blk)
        self.assertIsNone(re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', blk), 'żadnego adresu e-mail w kodzie zbieracza')

    def test_kontrola_straz_workflow_and_page(self):
        import importlib.util, tempfile
        root = os.path.dirname(os.path.abspath(__file__)); tmp = tempfile.mkdtemp(prefix='kontrola-ins-')
        with mock.patch.dict(os.environ, {'KONTROLA_DIR': os.path.join(tmp, 'kontrola'), 'KONTROLA_ARCH': os.path.join(tmp, 'archiwum')}, clear=False):
            spec = importlib.util.spec_from_file_location('v121_kontrola', os.path.join(root, 'narzedzia', 'kontrola.py'))
            k = importlib.util.module_from_spec(spec); spec.loader.exec_module(k)
            spec = importlib.util.spec_from_file_location('v121_straz', os.path.join(root, 'narzedzia', 'straz_kluczy.py'))
            s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
        self.assertIn('insider', k.PLIKI); self.assertEqual(k.LIMIT_MIN['insider'], 48 * 60)
        self.assertIn('SEC_CONTACT', s.NAZWY); self.assertEqual(sorted(s.sekrety({'SEC_CONTACT': self.C})[0]), ['SEC_CONTACT'], 'straż pilnuje adresu kontaktowego jak klucza')
        wf = self._rd(os.path.join(root, '.github', 'workflows', 'strona.yml'))
        self.assertEqual(wf.count('SEC_CONTACT: ${{ secrets.SEC_CONTACT }}'), 2, 'sekret w kroku zbieracza i w straży kluczy')
        i_z, i_s = wf.index('- name: Zbierz dane'), wf.index('- name: Straż kluczy')
        self.assertTrue(i_z < wf.index('SEC_CONTACT:', i_z) < wf.index('run: python3 zbieraj_dane.py') < i_s < wf.rindex('SEC_CONTACT:') < wf.index('python3 narzedzia/straz_kluczy.py'))
        html = self._rd(os.path.join(root, 'index.html'))
        self.assertEqual(html.count('<section class="panel pcard" id="g-insider" hidden></section>'), 1); self.assertIn("srvJSON('insider')", html)
        self.assertEqual(html.count('const EXTRA112='), 1); self.assertIn('for(const l in EXTRA112)if(I18N[l])Object.assign(I18N[l],EXTRA112[l]);', html)
        self.assertEqual(html.count('/* v121 insiderzy */'), 1); self.assertEqual(html.count('/* ===================== v121: INSIDERZY SPÓŁEK USA'), 1)


import io as _io_v121


class StresV121(unittest.TestCase):
    """v121 (obszar stres-opcje): indeks stresu finansowego (CSV rządu USA, domena publiczna) i put/call (JSON giełdy opcji — tylko ze zgodą
    CBOE_ZGODA, bo warunki giełdy wymagają umowy): brak ≠ zero, część z błędem = poprzednia wersja z własnym czasem, historia put/call
    dopełniana po 10 dni na przebieg, dni bez sesji zapamiętane tylko po XML magazynu plików (403 z inną treścią = błąd, nie święto),
    plik młodszy niż 6 h z pamięci; żadnego dostępu do sieci (urlopen zablokowany)."""
    NOW = datetime.datetime(2026, 9, 26, 14, 0, tzinfo=datetime.timezone.utc)   # sobota
    STUBS = tuple(n for n in dir(zd) if n.startswith('build_') and n != 'build_stres')   # pozostałe źródła udają awarię (bez sieci)
    CSV = ('﻿Date,OFR FSI,Credit,Equity valuation,Safe assets,Funding,Volatility,United States,Other advanced economies,Emerging markets\n'
           '2026-09-14,-2.4,-1.0,-0.5,-0.3,-0.1,-0.5,-1.2,-0.8,-0.5\n'
           '2026-09-15,-2.5,-1.1,-0.5,-0.3,-0.1,-0.5,-1.2,-0.8,-0.5\n'
           '2026-09-16,-2.6,-1.1,-0.55,-0.3,-0.12,-0.52,-1.25,-0.8,-0.5\n'
           '2026-09-17,,-1.1,-0.55,-0.3,-0.12,-0.52,-1.25,-0.8,-0.5\n'
           '2026-09-18,-2.7,-1.15,-0.56,-0.3,-0.13,-0.55,-1.3,-0.82,-0.55\n'
           '2026-09-21,-2.727,-1.15,-0.573,-0.296,-0.133,-0.574,-1.343,-0.827,-0.557\n'
           '2026-09-22,-2.773,-1.149,-0.577,-0.296,-0.145,-0.606,-1.371,-0.842,-0.56\n'
           'zły wiersz,1,2\n'
           '2026-09-23,-2.663,,-0.567,-0.294,-0.166,-0.488,-1.33,-0.772,-0.562\n')
    PC = {'ratios': [{'name': 'TOTAL PUT/CALL RATIO', 'value': '0.75'}, {'name': 'INDEX PUT/CALL RATIO', 'value': '0.95'},
                     {'name': 'EXCHANGE TRADED PRODUCTS PUT/CALL RATIO', 'value': '0.77'}, {'name': 'EQUITY PUT/CALL RATIO', 'value': '0.52'},
                     {'name': 'OEX PUT/CALL RATIO', 'value': '0.00'}, 'x', {'name': 'VIX', 'value': None}],
          'SUM OF ALL PRODUCTS': [{'name': 'VOLUME', 'call': 7971692, 'put': 5968172, 'total': 13939864}]}

    # treści odpowiedzi 403/404 (nagrane 26.09.2026): XML magazynu plików = pliku dla dnia nie ma; strona blokady sieci CDN = błąd
    S3 = b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code><Message>Access Denied</Message><RequestId>5THGK6X830JD2MRM</RequestId></Error>'
    NSK = b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message></Error>'
    CF = b'<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head><body>Sorry, you have been blocked</body></html>'

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()
        self.p_net = mock.patch.object(zd.urllib.request, 'urlopen', side_effect=AssertionError('sieć zabroniona w testach')); self.p_net.start()

    def tearDown(self):
        self.p_net.stop()

    @classmethod
    def _http(cls, code, body=None):   # świeży wyjątek (treść da się przeczytać raz); domyślnie XML magazynu plików = „pliku nie ma”
        return zd.urllib.error.HTTPError('u', code, 'x', {}, _io_v121.BytesIO(cls.S3 if body is None else body))

    @classmethod
    def _raise(cls, code, body=None):   # side_effect: każde wywołanie = nowy wyjątek z pełną treścią
        def f(*a, **k):
            raise cls._http(code, body)
        return f

    def test_fsi_parse_missing_is_not_zero(self):
        f = zd.fsi_parse(self.CSV)
        self.assertEqual((f['date'], f['value']), ('2026-09-23', -2.663))
        self.assertEqual(f['d1'], 0.11, 'wobec poprzedniego dnia serii (22.09)'); self.assertEqual(f['d5'], -0.163, 'wobec 5 dni serii wstecz (15.09; 17.09 bez wartości wypada)')
        self.assertEqual([r[0] for r in f['hist']], ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23'], 'dzień bez liczby głównej wypada, nie zero; zły wiersz pominięty')
        self.assertEqual(f['hist'][-1], ['2026-09-23', -2.663])
        self.assertEqual(f['cols']['credit'], {'v': None, 'd1': None}, 'składowa bez liczby → brak, nie zero')
        self.assertEqual(f['cols']['equity'], {'v': -0.567, 'd1': 0.01}); self.assertEqual(f['cols']['vol']['d1'], 0.118); self.assertEqual(f['cols']['em']['v'], -0.562)
        self.assertEqual(sorted(f['cols']), ['ae', 'credit', 'em', 'equity', 'funding', 'safe', 'us', 'vol'])
        g = zd.fsi_parse(self.CSV, dni=3)
        self.assertEqual(len(g['hist']), 3); self.assertEqual(g['d1'], 0.11); self.assertIsNone(g['d5'], 'za krótka seria: brak, nie zero')
        h = zd.fsi_parse('Date,OFR FSI,Funding\n2026-09-23,1.5,\n')
        self.assertEqual((h['value'], h['d1'], h['cols']), (1.5, None, {'funding': {'v': None, 'd1': None}}), 'tylko obecne kolumny; jedna obserwacja = bez zmian')
        for bad in ('', 'Data,X\n1,2\n', 'Date,OFR FSI\n2026-09-23,\n', 'Date,OFR FSI\nzle,1\n'):
            with self.assertRaises(ValueError):
                zd.fsi_parse(bad)

    def test_pc_parse_zero_is_missing(self):
        self.assertEqual(zd.pc_parse(self.PC), {'total': 0.75, 'equity': 0.52, 'index': 0.95})
        self.assertEqual(zd.pc_parse({'ratios': [{'name': 'Total Put/Call Ratio', 'value': '1,02'}, {'name': 'EQUITY PUT/CALL RATIO', 'value': '0.00'}, {'name': 'INDEX PUT/CALL RATIO', 'value': 'n/a'}]}),
                         {'total': 1.02, 'equity': None, 'index': None}, '„0.00” i tekst = brak, nie zero; przecinek dziesiętny przyjęty')
        for bad in ({}, [], {'ratios': 'x'}, {'ratios': []}, {'ratios': [{'name': 'TOTAL PUT/CALL RATIO', 'value': '0.00'}]}, {'ratios': [{'name': 'OEX PUT/CALL RATIO', 'value': '1'}]}):
            with self.assertRaises(ValueError):
                zd.pc_parse(bad)

    def test_pc_dni_weekdays_window_and_limit(self):
        D = datetime.date
        self.assertEqual(zd.pc_dni({'2026-09-24'}, {'2026-09-23'}, D(2026, 9, 26), n=4), [D(2026, 9, 25), D(2026, 9, 22), D(2026, 9, 21), D(2026, 9, 18)],
                         'sobota: od piątku wstecz, bez dnia w historii (24) i dnia bez sesji (23), bez weekendu, najwyżej n')
        self.assertEqual(zd.pc_dni(set(), set(), D(2026, 9, 28), n=2), [D(2026, 9, 28), D(2026, 9, 25)], 'poniedziałek: dziś, potem piątek')
        self.assertEqual(zd.pc_dni({'2026-09-21', '2026-09-22', '2026-09-24'}, set(), D(2026, 9, 26), n=10, dni=3), [D(2026, 9, 25), D(2026, 9, 23)],
                         'pełna historia (≥ dni wierszy): nie starsze niż jej najstarszy dzień')
        far = zd.pc_dni(set(), set(), D(2026, 9, 26), n=1000, dni=300)
        self.assertTrue(300 < len(far) < 340 and far[-1] >= D(2026, 9, 26) - datetime.timedelta(days=460) and all(d.weekday() < 5 for d in far), 'okno ≈ 1,5 × 300 + 10 dni kalendarzowych')
        self.assertEqual(zd.pc_prog(D(2026, 9, 26)), D(2026, 9, 26) - datetime.timedelta(days=460))

    def test_pc_czesc_backfill_missing_days_and_errors(self):
        def pc(v):
            return {'ratios': [{'name': 'TOTAL PUT/CALL RATIO', 'value': str(v)}, {'name': 'EQUITY PUT/CALL RATIO', 'value': str(round(v - 0.2, 2))}, {'name': 'INDEX PUT/CALL RATIO', 'value': '0.00'}]}
        resp = {'2026-09-25': 403, '2026-09-24': pc(0.8), '2026-09-23': (404, self.NSK), '2026-09-22': pc(0.7), '2026-09-21': pc(0.9), '2026-09-18': pc(1.1), '2026-09-17': 500}
        calls, naps = [], []

        def gj(url, headers=None, timeout=30):
            self.assertLessEqual(timeout, zd.PC_TIMEOUT); self.assertTrue(url.startswith(zd.PC_URL.split('{')[0]) and url.endswith('_daily_options'), url)
            d = url.rsplit('/', 1)[1][:10]; calls.append(d)
            r = resp.get(d, 404)
            if isinstance(r, (int, tuple)):
                raise self._http(*r) if isinstance(r, tuple) else self._http(r)
            return r
        prev = {'hist': [['2026-09-16', 0.6, 0.4, 0.9], ['2026-09-15', 0.65, None, 0.95], ['zła', 1, 1, 1], ['2026-09-14', 0, -1, 'x'], 'x'], 'brak': ['2026-09-07', 'x', 3]}
        errs = []
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            p, got = zd.pc_czesc(prev, self.NOW, errs, sleep=naps.append)
        self.assertEqual(calls, ['2026-09-25', '2026-09-24', '2026-09-23', '2026-09-22', '2026-09-21', '2026-09-18', '2026-09-17'],
                         'dni robocze od dziś wstecz, bez dni w historii (16, 15, 14) i bez zapamiętanego dnia bez sesji (7.09); HTTP 500 = koniec części')
        self.assertEqual(got, 4); self.assertEqual(errs, ['put/call 2026-09-17: HTTP 500']); self.assertEqual(naps, [zd.PC_TEMPO] * 6, 'odstęp między zapytaniami')
        self.assertEqual((p['date'], p['total'], p['equity'], p['index']), ('2026-09-24', 0.8, 0.6, None), 'najnowszy pobrany dzień; „0.00” = brak, nie zero')
        self.assertEqual([r[0] for r in p['hist']], ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-24'], 'rosnąco; śmieci odrzucone')
        self.assertEqual(p['hist'][0], ['2026-09-14', None, None, None], 'stare zero, liczba ujemna i tekst → brak'); self.assertEqual(p['n'], 7)
        self.assertEqual(p['brak'], ['2026-09-07', '2026-09-23'], '23.09 (3 dni temu, 404 NoSuchKey) = dzień bez sesji; 25.09 (wczoraj, 403 AccessDenied) = plik może dopiero powstać — bez zapisu')
        errs2, naps2 = [], []
        with mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd, 'PC_DNI', 3):
            q, got2 = zd.pc_czesc(p, self.NOW, errs2, sleep=naps2.append)
        self.assertEqual([r[0] for r in q['hist']], ['2026-09-21', '2026-09-22', '2026-09-24'], 'obcięte do PC_DNI ostatnich sesji'); self.assertEqual(got2, 0)
        self.assertEqual(errs2, ['put/call 2026-09-17: HTTP 500'], 'pełna historia: pytany tylko 25.09 (dopiero powstaje) i 17.09 (luka w oknie)')
        self.assertEqual(q['brak'], ['2026-09-23'], 'dni bez sesji przycięte do okna (7.09 poza oknem 3 sesji)')
        with mock.patch.object(zd, 'get_json', side_effect=self._raise(403)), self.assertRaises(ValueError):
            zd.pc_czesc(None, self.NOW, [], sleep=lambda s: None)
        with mock.patch.object(zd, 'get_json', side_effect=RuntimeError('padło')):
            r, g = zd.pc_czesc({'hist': [['2026-09-24', 0.8, 0.6, 0.9]]}, self.NOW, errs3 := [], sleep=lambda s: None)
        self.assertEqual((r['date'], g), ('2026-09-24', 0)); self.assertEqual(errs3, ['put/call 2026-09-25: padło'], 'inny błąd = koniec części, co jest — zostaje')

    def test_pc_403_without_storage_xml_is_error_not_holiday(self):
        self.assertTrue(zd.pc_brak_pliku(self._http(403)) and zd.pc_brak_pliku(self._http(404, self.NSK)), 'XML magazynu plików = pliku nie ma')
        for body in (self.CF, b'', b'error code: 1020', b'<Error><Code>SlowDown</Code></Error>'):
            self.assertFalse(zd.pc_brak_pliku(self._http(403, body)), body[:30])
        bad = mock.Mock(); bad.read.side_effect = OSError('zerwane połączenie')
        self.assertFalse(zd.pc_brak_pliku(bad), 'błąd odczytu treści = nie wiemy = błąd')
        # stary dzień z blokadą CDN: błąd i koniec części; dzień NIE trafia do dni bez sesji, historia zostaje
        resp = {'2026-09-25': 403, '2026-09-23': (403, self.CF), '2026-09-22': 403}
        calls = []

        def gj(url, headers=None, timeout=30):
            d = url.rsplit('/', 1)[1][:10]; calls.append(d)
            r = resp.get(d, 403)
            raise self._http(*r) if isinstance(r, tuple) else self._http(r)
        prev = {'hist': [['2026-09-24', 0.8, 0.6, 0.9]], 'brak': ['2026-09-07']}
        with mock.patch.object(zd, 'get_json', side_effect=gj):
            p, got = zd.pc_czesc(prev, self.NOW, errs := [], sleep=lambda s: None)
        self.assertEqual(calls, ['2026-09-25', '2026-09-23'], 'po blokadzie koniec zapytań w tym przebiegu')
        self.assertEqual(errs, ['put/call 2026-09-23: HTTP 403 bez znacznika braku pliku']); self.assertEqual(got, 0)
        self.assertEqual(p['brak'], ['2026-09-07'], 'ani 23.09 (blokada), ani 25.09 (młody) nie są dniami bez sesji')
        self.assertEqual(p['hist'], [['2026-09-24', 0.8, 0.6, 0.9]])
        # młody dzień z pustą odpowiedzią 404 — też błąd (nie „plik dopiero powstaje”)
        with mock.patch.object(zd, 'get_json', side_effect=self._raise(404, b'')):
            q, _g = zd.pc_czesc(prev, self.NOW, errs2 := [], sleep=lambda s: None)
        self.assertEqual(errs2, ['put/call 2026-09-25: HTTP 404 bez znacznika braku pliku']); self.assertEqual(q['brak'], ['2026-09-07'])
        # w budowniczym: część pc z błędem (ok False), dane i czas części z poprzedniego pliku, błąd w META
        prev_f = {'at': '2026-09-26T07:00:00+00:00', 'part_at': {'fsi': '2026-09-26T07:00:00+00:00', 'pc': '2026-09-25T07:00:00+00:00'}, 'pc': dict(p, date='2026-09-24')}
        with mock.patch.object(zd, 'get', return_value=(200, self.CSV)), mock.patch.object(zd, 'get_json', side_effect=self._raise(403, self.CF)), \
                mock.patch.object(zd, 'PC_ZGODA', 'x'), mock.patch.object(zd, 'NOW', self.NOW.isoformat()), mock.patch.object(zd.time, 'sleep', lambda s: None):
            o = zd.build_stres(prev_f, self.NOW)
        self.assertEqual(o['ok'], {'fsi': True, 'pc': False}); self.assertEqual(o['part_at']['pc'], '2026-09-25T07:00:00+00:00'); self.assertEqual(o['pc']['brak'], ['2026-09-07'])
        self.assertEqual(zd.META['errors'], ['Stres: put/call 2026-09-25: HTTP 403 bez znacznika braku pliku'])

    def test_build_parts_independent_and_consent_gate(self):
        urls = []

        def gt(url, headers=None, timeout=30):
            urls.append((url, timeout)); return 200, self.CSV
        with mock.patch.object(zd, 'get', side_effect=gt), mock.patch.object(zd, 'get_json', side_effect=AssertionError('bez zgody nie pytać o put/call')), \
                mock.patch.object(zd, 'PC_ZGODA', ''), mock.patch.object(zd, 'NOW', self.NOW.isoformat()):
            o = zd.build_stres(None, self.NOW)
        self.assertEqual(urls, [(zd.FSI_URL, zd.FSI_TIMEOUT)])
        self.assertEqual(o['ok'], {'fsi': True}); self.assertTrue(o['pc_off']); self.assertNotIn('pc', o); self.assertEqual(o['part_at'], {'fsi': self.NOW.isoformat()})
        self.assertEqual((o['fsi']['date'], o['fsi']['value'], o['fsi']['d1'], len(o['fsi']['hist'])), ('2026-09-23', -2.663, 0.11, 7)); self.assertEqual(zd.META['errors'], [])
        self.assertNotIn('put/call', o['src']); self.assertEqual(zd.stres_czesci(), ('fsi',))
        self.assertNotIn('Cboe', json.dumps(o)); self.assertNotIn('OFR', o['src'], 'nazwy dostawców tylko na stronie Źródła')
        # ze zgodą: obie części; część pc dopełniona z poprzedniego pliku (24.09 już jest; 25.09 pobrany; reszta okna bez pliku = dni bez sesji)
        prev = {'at': '2026-09-26T07:00:00+00:00', 'ok': {'fsi': True}, 'part_at': {'fsi': '2026-09-26T07:00:00+00:00'}, 'fsi': {'date': '2026-09-22', 'value': -2.773, 'hist': []},
                'pc': {'hist': [['2026-09-24', 0.8, 0.6, 0.9]], 'brak': []}}

        def gj(url, headers=None, timeout=30):
            if url.endswith('2026-09-25_daily_options'):
                return self.PC
            raise self._http(403)
        with mock.patch.object(zd, 'get', side_effect=gt), mock.patch.object(zd, 'get_json', side_effect=gj), mock.patch.object(zd, 'PC_ZGODA', 'umowa 2026-10-01'), \
                mock.patch.object(zd, 'NOW', self.NOW.isoformat()), mock.patch.object(zd.time, 'sleep', lambda s: None):
            o2 = zd.build_stres(prev, self.NOW); czesci = zd.stres_czesci()
        self.assertEqual(o2['ok'], {'fsi': True, 'pc': True}); self.assertFalse(o2['pc_off']); self.assertEqual(czesci, ('fsi', 'pc')); self.assertIn('put/call', o2['src'])
        self.assertEqual((o2['pc']['date'], o2['pc']['total'], o2['pc']['equity'], o2['pc']['index']), ('2026-09-25', 0.75, 0.52, 0.95))
        self.assertEqual([r[0] for r in o2['pc']['hist']], ['2026-09-24', '2026-09-25']); self.assertEqual(len(o2['pc']['brak']), 9, '10 dni na przebieg: 9 bez pliku starszych niż 2 dni')
        self.assertEqual(o2['part_at'], {'fsi': self.NOW.isoformat(), 'pc': self.NOW.isoformat()}); self.assertEqual(zd.META['errors'], [])
        # awaria obu źródeł: poprzednie części z własnym czasem; błąd w META, nigdy zera
        with mock.patch.object(zd, 'get', side_effect=RuntimeError('timeout')), mock.patch.object(zd, 'get_json', side_effect=RuntimeError('padło')), \
                mock.patch.object(zd, 'PC_ZGODA', 'x'), mock.patch.object(zd, 'NOW', self.NOW.isoformat()), mock.patch.object(zd.time, 'sleep', lambda s: None):
            o3 = zd.build_stres(o2, self.NOW)
        self.assertEqual(o3['ok'], {'fsi': False, 'pc': False}); self.assertIs(o3['fsi'], o2['fsi']); self.assertEqual(o3['part_at'], o2['part_at'], 'części z błędem: poprzednie z własnym czasem')
        self.assertEqual(o3['pc']['hist'], o2['pc']['hist'], 'put/call: historia zostaje, nowy dzień nie doszedł')
        self.assertEqual(zd.META['errors'], ['Stres: indeks stresu: timeout; put/call 2026-09-10: padło'], '24 i 25.09 już są, 11–23.09 zapamiętane jako dni bez sesji → pytany 10.09')
        # bez zgody stare put/call nie są przepisywane; zgoda + brak jakichkolwiek danych pc = część z błędem bez klucza pc
        with mock.patch.object(zd, 'get', side_effect=gt), mock.patch.object(zd, 'PC_ZGODA', ''), mock.patch.object(zd, 'NOW', self.NOW.isoformat()):
            o4 = zd.build_stres(o2, self.NOW)
        self.assertNotIn('pc', o4); self.assertTrue(o4['pc_off']); self.assertEqual(o4['ok'], {'fsi': True})
        zd.META['errors'].clear()
        with mock.patch.object(zd, 'get', side_effect=gt), mock.patch.object(zd, 'get_json', side_effect=self._raise(403)), mock.patch.object(zd, 'PC_ZGODA', 'x'), \
                mock.patch.object(zd, 'NOW', self.NOW.isoformat()), mock.patch.object(zd.time, 'sleep', lambda s: None):
            o5 = zd.build_stres(None, self.NOW)
        self.assertEqual(o5['ok'], {'fsi': True, 'pc': False}); self.assertNotIn('pc', o5); self.assertEqual(zd.META['errors'], ['Stres: put/call: brak danych'])
        with mock.patch.object(zd, 'get', side_effect=RuntimeError('timeout')), mock.patch.object(zd, 'PC_ZGODA', ''), self.assertRaises(RuntimeError):
            zd.build_stres(None, self.NOW)

    def test_main_wiring_cache_consent_and_failure(self):
        stubs = [mock.patch.object(zd, f, side_effect=RuntimeError('offline'), create=True) for f in self.STUBS]
        for s in stubs:
            s.start()
        try:
            saved, prev, calls = {}, {}, []
            env = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY',
                                   'EODHD_KEY', 'MASSIVE_KEY', 'TIINGO_KEY', 'FMP_KEY', 'ALPHAVANTAGE_KEY', 'ETHERSCAN_KEY', 'SITE_URL', 'CACHE_DIR')}
            built = {'at': zd.NOW, 'ok': {'fsi': True}, 'part_at': {'fsi': zd.NOW}, 'fsi': {'date': '2026-09-23', 'value': -2.663}, 'pc_off': True}
            built2 = {'at': zd.NOW, 'ok': {'fsi': True, 'pc': True}, 'part_at': {'fsi': zd.NOW, 'pc': zd.NOW}, 'fsi': {'date': '2026-09-23', 'value': -2.663}, 'pc': {'date': '2026-09-25', 'total': 0.75}, 'pc_off': False}

            def run(zgoda, builder):
                with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                        mock.patch.object(zd, 'previous', lambda n: prev.get(n)), mock.patch.object(zd, 'build_stres', builder), mock.patch.object(zd, 'PC_ZGODA', zgoda):
                    zd.main()
            run('', lambda p, now=None: calls.append(p) or built)
            self.assertEqual(calls, [None]); self.assertIs(saved['stres'], built); self.assertIs(zd.META['ok']['stres'], True)
            self.assertIn('Stres: część put/call wyłączona (zmienna CBOE_ZGODA pusta)', zd.META['notes'], 'brak zgody = neutralna informacja'); self.assertFalse(any(e.startswith('Stres') for e in zd.META['errors']))
            # młodszy niż 6 h, wszystkie oczekiwane części w porządku, ten sam stan zgody → z pamięci
            prev['stres'] = {'at': _iso(10), 'ok': {'fsi': True}, 'pc_off': True}; calls.clear(); zd.META['ok'].clear(); zd.META['notes'].clear()
            run('', mock.Mock(side_effect=AssertionError('plik młodszy niż 6 h — nie budować')))
            self.assertIs(saved['stres'], prev['stres']); self.assertEqual(zd.META['ok']['stres'], 'cached')
            # pojawiła się zgoda → przebudowa mimo młodego pliku (część pc oczekiwana); bez noty o braku zgody
            zd.META['notes'].clear()
            run('umowa 2026-10-01', lambda p, now=None: calls.append(p) or built2)
            self.assertEqual(calls, [prev['stres']]); self.assertIs(saved['stres'], built2); self.assertIs(zd.META['ok']['stres'], True)
            self.assertFalse(any('CBOE_ZGODA' in n for n in zd.META['notes']))
            # zgoda cofnięta → przebudowa (stare put/call nie zostają); młody plik z częścią pc z błędem: ponowienie dopiero po godzinie
            prev['stres'] = dict(built2, at=_iso(10)); calls.clear()
            run('', lambda p, now=None: calls.append(p) or built)
            self.assertEqual(len(calls), 1, 'zmiana stanu zgody = przebudowa')
            prev['stres'] = {'at': _iso(10), 'ok': {'fsi': True, 'pc': False}, 'pc_off': False}; calls.clear(); zd.META['ok'].clear()
            run('x', mock.Mock(side_effect=AssertionError('część z błędem młodsza niż godzina — nie budować')))
            self.assertEqual(zd.META['ok']['stres'], 'cached')
            prev['stres']['at'] = _iso(75)
            run('x', lambda p, now=None: calls.append(p) or built2)
            self.assertEqual(len(calls), 1, 'po godzinie część z błędem ponowiona')
            # awaria → poprzedni plik i błąd; częściowy wynik (część pc z błędem) = ok False
            prev['stres']['at'] = _iso(400); zd.META['ok'].clear(); zd.META['errors'].clear()
            run('x', mock.Mock(side_effect=RuntimeError('offline')))
            self.assertIs(saved['stres'], prev['stres']); self.assertIs(zd.META['ok']['stres'], False); self.assertIn('Stres: offline', zd.META['errors'])
            run('x', lambda p, now=None: dict(built2, ok={'fsi': True, 'pc': False}))
            self.assertIs(zd.META['ok']['stres'], False)
            prev.clear(); saved.clear(); zd.META['ok'].clear()
            run('', mock.Mock(side_effect=RuntimeError('offline')))
            self.assertNotIn('stres', saved, 'bez poprzedniego pliku i z awarią — żadnego pliku (nie pustka)'); self.assertIs(zd.META['ok']['stres'], False)
        finally:
            for s in stubs:
                s.stop()

    def test_kontrola_page_docs_and_flow_stubs(self):
        import re as _re
        root = os.path.dirname(os.path.abspath(__file__))

        def rd(p):
            with open(p, encoding='utf-8') as f:
                return f.read()
        k = rd(os.path.join(root, 'narzedzia', 'kontrola.py'))
        self.assertRegex(k, r"PLIKI = \[[^\]]*'stres'[^\]]*\]"); self.assertRegex(k, r"LIMIT_MIN = \{[^}]*'stres': 24 \* 60[^}]*\}")
        html = rd(os.path.join(root, 'index.html'))
        self.assertIn("srvJSON('stres')", html); self.assertEqual(html.count('<section class="panel pcard" id="g-stres" hidden></section>'), 1)
        src = rd(zd.__file__)
        self.assertIn("prev_st = previous('stres')", src); self.assertIn("META['errors'].append(mask(f'Stres: {e}')); META['ok']['stres'] = False", src)
        self.assertIn("if prev_st: save('stres', prev_st)", src); self.assertLess(src.index("prev_st = previous('stres')"), src.index("prev_ix = previous('indeksy')"))
        self.assertIn('CBOE_ZGODA', zd.build_stres.__doc__); self.assertIn('Use of Content', zd.build_stres.__doc__, 'ustalenie o warunkach giełdy zapisane w docstringu')
        me = rd(__file__); a = me.index('class StresV121('); b = me.find('\nclass ', a + 1); b = len(me) if b < 0 else b; rest = me[:a] + me[b:]
        tups = [m.group(0) for m in _re.finditer(r"\('build_\w+'(?:,\s*'build_\w+')+\)", rest) if "'build_wieloryby'" in m.group(0)]
        self.assertTrue(len(tups) >= 18 and all("'build_stres', 'build_wieloryby'" in x for x in tups), 'każda lista zaślepek przebiegu głównego (z build_wieloryby) zna build_stres')
        i = rest.index('def test_meta_is_always_written'); j = rest.index('\n    def ', i + 1)
        self.assertRegex(rest[i:j], r"startswith\(\('instytucje', 'Stres', ", 'etykieta „Stres” w filtrze błędów meta (po „instytucje”)')
        self.assertEqual(zd.pc_parse.__doc__.count('None'), 1); self.assertTrue(zd.FSI_URL.startswith('https://') and zd.PC_URL.startswith('https://'))


class AukcjeV121(unittest.TestCase):
    """v121: aukcje papierów skarbowych USA — wiersze z danych fiskalnych i z serwisu aukcyjnego, przyszłe aukcje i CMB pominięte,
    udziały od części konkurencyjnej (razem 100%), termin bieżący gdy pełny (pierwotny tylko przy niepełnym terminie dodatkowej transzy),
    data niemożliwa pominięta, rentowność wg rodzaju, mediany 12 miesięcy per papier (≥ 3 aukcje), zapas po awarii źródła głównego (także
    częściowy), przebieg główny (co 6 h, ponowienie po godzinie, poprzedni plik po awarii), kontrola (PLIKI, limit 24 h).
    Bez sieci: get_json zaślepione."""
    NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)

    def setUp(self):
        zd.META['errors'].clear(); zd.META['ok'].clear(); zd.META['notes'].clear()

    @staticmethod
    def _fd(date, typ, term, btc, ind, dr, pd, tot, hy=None, hi=None, hm=None, org=None, reopen='No', cmb='No', frn='No', tips='No', cusip='X', comp='auto'):
        f = lambda v: 'null' if v is None else (f'{v:.6f}' if isinstance(v, float) else str(v))   # noqa: E731 — pola tekstowe jak w interfejsie
        if comp == 'auto':
            comp = None if None in (ind, dr, pd) else ind + dr + pd
        return {'auction_date': date, 'security_type': typ, 'security_term': term, 'original_security_term': org or term, 'reopening': reopen,
                'cash_management_bill_cmb': cmb, 'floating_rate': frn, 'inflation_index_security': tips, 'bid_to_cover_ratio': f(btc),
                'comp_accepted': f(comp), 'indirect_bidder_accepted': f(ind), 'direct_bidder_accepted': f(dr), 'primary_dealer_accepted': f(pd),
                'total_accepted': f(tot), 'high_yield': f(hy), 'high_investment_rate': f(hi), 'high_discnt_margin': f(hm), 'cusip': cusip}

    @staticmethod
    def _td(row):
        """Ten sam wiersz w kształcie serwisu aukcyjnego (camelCase, rodzaj w polu type, data z godziną)."""
        typ = row['security_type']
        if row['cash_management_bill_cmb'] == 'Yes': typ = 'CMB'
        elif row['floating_rate'] == 'Yes': typ = 'FRN'
        elif row['inflation_index_security'] == 'Yes': typ = 'TIPS'
        n = lambda v: '' if v == 'null' else v   # noqa: E731 — serwis aukcyjny daje pusty tekst zamiast „null”
        return {'auctionDate': row['auction_date'] + 'T00:00:00', 'type': typ, 'securityType': row['security_type'], 'securityTerm': row['security_term'],
                'originalSecurityTerm': row['original_security_term'], 'reopening': row['reopening'], 'bidToCoverRatio': n(row['bid_to_cover_ratio']),
                'competitiveAccepted': n(row['comp_accepted']), 'indirectBidderAccepted': n(row['indirect_bidder_accepted']),
                'directBidderAccepted': n(row['direct_bidder_accepted']), 'primaryDealerAccepted': n(row['primary_dealer_accepted']),
                'totalAccepted': n(row['total_accepted']), 'highYield': n(row['high_yield']), 'highInvestmentRate': n(row['high_investment_rate']),
                'highDiscountMargin': n(row['high_discnt_margin']), 'cusip': row['cusip']}

    def _data(self):
        F = self._fd
        rows = [F('2026-09-29', 'Bill', '52-Week', None, None, None, None, None, cusip='FUT1'),                                     # przyszła: bez wyników
                F('2026-09-28', 'Note', '2-Year', 0.0, 1.0, 1.0, 1.0, 3.0, hy=4.0, cusip='ZERO'),                                    # zero nie jest wynikiem
                F('2026-09-24', 'Note', '7-Year', 2.42, 24869544500, 13163710000, 5447550000, 50624250500, hy=5.085, cusip='91282CRM5'),   # prawdziwa (24.09.2026)
                F('2026-09-24', 'Bill', '8-Week', 2.76, 48063244000, 4915000000, 28753160000, 92026009800, hi=4.071, org='17-Week', reopen='Yes', cusip='912797VY0'),
                F('2026-09-23', 'Note', '1-Year 10-Month', 2.63, 16537912500, 5000000000, 6451850000, 28000087000, hm=0.04, org='2-Year', reopen='Yes', frn='Yes', cusip='FRN1'),
                F('2026-09-17', 'Note', '9-Year 10-Month', 2.24, 11164960000, 3000000000, 4719024000, 21860454000, hy=2.653, org='10-Year', reopen='Yes', tips='Yes', cusip='TIPS1'),
                F('2026-09-09', 'Note', '9-Year 11-Month', 2.71, 30804090000, 4000000000, 3000000000, 39000040000, hy=4.834, org='10-Year', reopen='Yes', cusip='N10R'),
                F('2026-09-02', 'Note', '2-Year', 2.60, 1.0, 1.0, 1.0, 69000000000, hy=4.7, cusip='N2A'),
                F('2026-08-05', 'Note', '2-Year', 2.50, None, 1.0, 1.0, 69000000000, hy=4.6, cusip='N2B', comp=3.0),                # bez kwoty pośrednich: udział = brak
                F('2026-05-21', 'Bill', '27-Day', 4.60, 3857175000, 1.0, 1.0, 25000301000, hi=3.665, cmb='Yes', cusip='CMB1'),      # CMB pominięty
                {'auction_date': 'bad', 'security_type': 'Bill', 'security_term': '4-Week', 'bid_to_cover_ratio': '2.5', 'total_accepted': '5'},
                'śmieć', None]
        # 7-latki co miesiąc (24. dnia): 2025-08-24 … 2026-08-24 (13 sztuk; okno 365 dni od 24.09.2026 zaczyna się 24.09.2025 → 2025-08-24 poza oknem)
        months = [(2025, m) for m in range(8, 13)] + [(2026, m) for m in range(1, 9)]
        for i, (y, m) in enumerate(months):
            rows.append(F(f'{y}-{m:02d}-24', 'Note', '7-Year', 2.30 + 0.01 * i, 25e9 + i * 1e8, 13e9, 6e9, 44e9, hy=4.5 + 0.01 * i, cusip=f'N7-{i}'))
        return rows

    def test_wiersze_z_danych_fiskalnych(self):
        w = zd.auk_wiersz(self._data()[2], 'fiscaldata')
        self.assertEqual((w['date'], w['type'], w['term'], w['k'], w['reopen'], w['btc']), ('2026-09-24', 'Note', '7-Year', 'Note 7-Year', False, 2.42))
        self.assertEqual((w['indirect_pct'], w['direct_pct'], w['dealer_pct']), (57.2, 30.3, 12.5), 'udziały od części konkurencyjnej: razem 100%')
        self.assertEqual((w['yield'], w['ykind'], w['accepted_bln'], w['cusip']), (5.085, 'yld', 50.624, '91282CRM5'))
        b = zd.auk_wiersz(self._data()[3], 'fiscaldata')
        self.assertEqual((b['type'], b['term'], b['k'], b['reopen'], b['yield'], b['ykind']), ('Bill', '8-Week', 'Bill 8-Week', False, 4.071, 'inv'), 'bon: termin bieżący, nie pierwotny 17-Week; „dodatkowa transza” bonu nie jest informacją')
        self.assertEqual((b['indirect_pct'], b['direct_pct'], b['dealer_pct']), (58.8, 6.0, 35.2))
        f = zd.auk_wiersz(self._data()[4], 'fiscaldata'); self.assertEqual((f['type'], f['term'], f['yield'], f['ykind']), ('FRN', '2-Year', 0.04, 'dm'))
        t = zd.auk_wiersz(self._data()[5], 'fiscaldata'); self.assertEqual((t['type'], t['term'], t['k'], t['ykind']), ('TIPS', '10-Year', 'TIPS 10-Year', 'real'))
        r = zd.auk_wiersz(self._data()[6], 'fiscaldata'); self.assertEqual((r['type'], r['term'], r['reopen']), ('Note', '10-Year', True), 'dodatkowa transza 10-latki = 10 lat')
        n = zd.auk_wiersz(self._data()[8], 'fiscaldata'); self.assertEqual((n['indirect_pct'], n['direct_pct'], n['dealer_pct']), (None, 33.3, 33.3), 'brak kwoty pośrednich = brak, nie 0')
        for i in (0, 1, 9, 10, 11, 12):
            self.assertIsNone(zd.auk_wiersz(self._data()[i], 'fiscaldata'), f'wiersz {i}: bez wyników, zero, CMB, zła data albo śmieć = pominięty')
        self.assertEqual([zd._auk_num(v) for v in ('2.420000', 'null', '', None, 'x', float('nan'), 3, True)], [2.42, None, None, None, None, None, 3.0, None])

    def test_termin_dodatkowej_transzy_i_data_niemozliwa(self):
        F = self._fd
        # prawdziwy wiersz danych fiskalnych z 26.01.2026: aukcja 2-latki przez dodatkową transzę 5-latki z 2023 r. (CUSIP 91282CGH8)
        r2 = F('2026-01-26', 'Note', '2-Year', 2.75, 44206733000, 19407500000, 5031125000, 73862272800, hy=3.58, org='5-Year', reopen='Yes', cusip='91282CGH8')
        w = zd.auk_wiersz(r2, 'fiscaldata')
        self.assertEqual((w['type'], w['term'], w['k'], w['reopen'], w['btc'], w['indirect_pct']), ('Note', '2-Year', 'Note 2-Year', True, 2.75, 64.4),
                         'termin pełny = termin bieżący: to aukcja 2-latki, nie 5-latki')
        self.assertEqual(zd.auk_wiersz(self._td(r2), 'treasurydirect'), w, 'zapas daje to samo')
        # termin niepełny dodatkowej transzy = termin pierwotny (wszystkie kształty z danych 26.09.2026)
        for typ, now_t, org, frn, tips, k in [('Note', '9-Year 11-Month', '10-Year', 'No', 'No', 'Note 10-Year'), ('Bond', '19-Year 10-Month', '20-Year', 'No', 'No', 'Bond 20-Year'),
                                              ('Bond', '29-Year 11-Month', '30-Year', 'No', 'No', 'Bond 30-Year'), ('Note', '1-Year 10-Month', '2-Year', 'Yes', 'No', 'FRN 2-Year'),
                                              ('Note', '4-Year 10-Month', '5-Year', 'No', 'Yes', 'TIPS 5-Year'), ('Note', '9-Year 8-Month', '10-Year', 'No', 'Yes', 'TIPS 10-Year'),
                                              ('Bond', '29-Year 6-Month', '30-Year', 'No', 'Yes', 'TIPS 30-Year'), ('Note', '', '3-Year', 'No', 'No', 'Note 3-Year')]:
            x = zd.auk_wiersz(F('2026-09-09', typ, now_t, 2.5, 6e10, 1e10, 1e10, 8e10, hy=4.0, hm=0.1, org=org, reopen='Yes', frn=frn, tips=tips), 'fiscaldata')
            self.assertEqual((x['k'], x['reopen']), (k, True), now_t)
        # mediany: 2-latka z dodatkowej transzy liczy się do 2-latek, 5-latka ma tylko prawdziwe 5-latki
        rows = [r2] + [F(f'2026-0{m}-24', 'Note', '2-Year', 2.6, 5e10, 1e10, 1e10, 7e10, hy=4.0, cusip=f'N2-{m}') for m in (2, 3)] \
            + [F(f'2026-0{m}-25', 'Note', '5-Year', 2.3, 5e10, 1e10, 1e10, 7e10, hy=4.0, cusip=f'N5-{m}') for m in (2, 3, 4)]
        med = zd.auk_mediany([zd.auk_wiersz(r, 'fiscaldata') for r in rows])
        self.assertEqual((med['Note 2-Year']['n'], med['Note 5-Year']['n'], med['Note 2-Year']['from']), (3, 3, '2026-01-26'))
        # data niemożliwa: wiersz pominięty (nie wywraca median ani całego przebiegu)
        for bad in ('2026-19-24', '2026-02-30', '0000-01-01'):
            self.assertIsNone(zd.auk_wiersz(dict(r2, auction_date=bad), 'fiscaldata'), bad)
            self.assertIsNone(zd.auk_wiersz(dict(self._td(r2), auctionDate=bad + 'T00:00:00'), 'treasurydirect'), bad)
        get_json, calls = self._get(fd=self._data() + [dict(self._data()[2], auction_date='2026-19-24', cusip='ZLA')])
        with mock.patch.object(zd, 'get_json', side_effect=get_json):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual((o['api'], o['to'], o['n'], zd.META['errors']), ('fiscaldata', '2026-09-24', 20, []), 'zła data nie zatrzymuje budowy')

    def test_wiersz_z_serwisu_aukcyjnego(self):
        for i in (2, 3, 4, 5, 6, 8):
            a, b = zd.auk_wiersz(self._data()[i], 'fiscaldata'), zd.auk_wiersz(self._td(self._data()[i]), 'treasurydirect')
            self.assertEqual(a, b, f'wiersz {i}: oba źródła dają ten sam kształt')
        self.assertIsNone(zd.auk_wiersz(self._td(self._data()[0]), 'treasurydirect')); self.assertIsNone(zd.auk_wiersz(self._td(self._data()[9]), 'treasurydirect'))

    def test_mediany(self):
        rows = [w for w in (zd.auk_wiersz(r, 'fiscaldata') for r in self._data()) if w]
        med = zd.auk_mediany(rows)
        self.assertEqual(sorted(med), ['Note 7-Year'], 'tylko papier z ≥ 3 aukcjami w oknie; 2-latki (2 sztuki), 10-latki (2) i reszta — bez mediany')
        m = med['Note 7-Year']
        self.assertEqual(m['n'], 13, '12 miesięcznych + 24.09.2026; 24.08.2025 poza oknem 365 dni')
        self.assertEqual((m['from'], m['to']), ('2025-09-24', '2026-09-24'))
        vals = sorted([2.30 + 0.01 * i for i in range(1, 13)] + [2.42]); self.assertEqual(m['btc'], round(statistics.median(vals), 2))
        self.assertAlmostEqual(m['indirect_pct'], round(statistics.median([round((25e9 + i * 1e8) / (25e9 + i * 1e8 + 19e9) * 100, 1) for i in range(1, 13)] + [57.2]), 1))
        self.assertEqual(zd.auk_mediany([]), {})
        few = [dict(r, indirect_pct=None) for r in rows if r['k'] == 'Note 7-Year']
        self.assertIsNone(zd.auk_mediany(few)['Note 7-Year']['indirect_pct'], 'mediana udziału bez liczb = brak, nie 0')

    def _get(self, fd=None, td=None):
        """Zaślepka get_json: adres danych fiskalnych → fd (lista albo wyjątek); serwis aukcyjny → td[rodzaj] (lista albo wyjątek)."""
        calls = []

        def get_json(url, headers=None, timeout=30):
            calls.append(url)
            if 'fiscaldata' in url:
                if isinstance(fd, Exception): raise fd
                return {'data': fd, 'meta': {'count': len(fd)}}
            if 'treasurydirect' in url:
                typ = url.split('type=')[1].split('&')[0]
                v = (td or {}).get(typ, ValueError('nieznany rodzaj ' + typ))
                if isinstance(v, Exception): raise v
                return v
            raise AssertionError('nieoczekiwany adres ' + url)
        return get_json, calls

    def test_build_z_danych_fiskalnych(self):
        get_json, calls = self._get(fd=self._data())
        with mock.patch.object(zd, 'get_json', side_effect=get_json), mock.patch.object(zd, 'NOW', '2026-09-26T12:00:00+00:00'):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual(len(calls), 1); self.assertIn('page[size]=1500', calls[0]); self.assertIn('filter=auction_date:gte:2025-08-22', calls[0]); self.assertIn('fields=auction_date,', calls[0])
        self.assertEqual((o['at'], o['api'], o['ok']), ('2026-09-26T12:00:00+00:00', 'fiscaldata', {'last': True, 'med12m': True, 'full': True}))
        self.assertEqual(zd.META['errors'], [])
        self.assertEqual([(r['date'], r['k']) for r in o['last'][:6]], [('2026-09-24', 'Note 7-Year'), ('2026-09-24', 'Bill 8-Week'), ('2026-09-23', 'FRN 2-Year'), ('2026-09-17', 'TIPS 10-Year'), ('2026-09-09', 'Note 10-Year'), ('2026-09-02', 'Note 2-Year')],
                         'od najnowszej; ten sam dzień w kolejności źródła; przyszłe, zero i CMB pominięte')
        self.assertEqual((o['n'], o['from'], o['to'], len(o['last'])), (20, '2025-08-24', '2026-09-24', 20))
        self.assertEqual(sorted(o['med12m']), ['Note 7-Year']); self.assertTrue(o['src'] and 'domena publiczna' in o['src'])
        self.assertTrue(any('brak nie jest zerem' in n for n in o['notes']) and any('CMB' in n for n in o['notes']))
        self.assertTrue(all(r['yield'] is None or r['yield'] > 0 for r in o['last']), 'żadna rentowność nie jest zerem')
        # więcej niż AUK_LAST aukcji: plik niesie tylko ostatnie 40, mediany z całości; powtórzony CUSIP tego samego dnia liczony raz
        big = self._data() + [self._fd(f'2026-0{1 + i // 28}-{1 + i % 28:02d}', 'Bill', '4-Week', 2.9, 5e10, 4e9, 3e10, 9e10, hi=4.0, cusip=f'B4-{i}') for i in range(40)]
        big.append(dict(self._data()[2]))   # duplikat 7-latki z 24.09
        get_json, calls = self._get(fd=big)
        with mock.patch.object(zd, 'get_json', side_effect=get_json):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual((len(o['last']), o['n']), (40, 60)); self.assertEqual(o['med12m']['Bill 4-Week']['n'], 40)
        self.assertEqual(sum(1 for r in o['last'] if r['cusip'] == '91282CRM5'), 1)

    def test_build_zapas_po_awarii_i_awaria_obu(self):
        td = {t: [self._td(r) for r in self._data()[:10] if isinstance(r, dict) and r.get('auction_date')] for t in zd.AUK_TD_TYPES}
        get_json, calls = self._get(fd=zd.urllib.error.HTTPError('u', 503, 'Service Unavailable', {}, None), td=td)
        with mock.patch.object(zd, 'get_json', side_effect=get_json), mock.patch.object(zd, 'NOW', '2026-09-26T12:00:00+00:00'):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual(len(calls), 6, 'jedno zapytanie główne + pięć rodzajów w zapasie'); self.assertTrue(all('startDate=08/22/2025&endDate=10/10/2026' in u for u in calls[1:]))
        self.assertEqual((o['api'], o['ok']), ('treasurydirect', {'last': True, 'med12m': False, 'full': False}), 'zbudowany z zapasu = część „full” bez zgody (ponowienie po godzinie)')
        self.assertEqual(len(o['last']), 7, 'każdy rodzaj odpowiada tą samą listą: aukcja liczona raz (CUSIP + dzień)')
        self.assertEqual(o['last'][0]['k'], 'Note 7-Year'); self.assertEqual(len(zd.META['errors']), 1); self.assertTrue(zd.META['errors'][0].startswith('Aukcje: dane fiskalne: HTTP Error 503'))
        # zapas częściowy: bony zawodzą — reszta zostaje, nota o braku rodzaju
        zd.META['errors'].clear()
        td2 = dict(td, Bill=zd.urllib.error.HTTPError('u', 403, 'Forbidden', {}, None))
        get_json, calls = self._get(fd=ValueError('odpowiedź bez listy „data”'), td=td2)
        with mock.patch.object(zd, 'get_json', side_effect=get_json):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual((o['api'], o['ok']['last'], o['ok']['full']), ('treasurydirect', True, False)); self.assertIn('zapas bez rodzajów: Bill', o['notes'])
        self.assertTrue('serwis aukcyjny Bill: HTTP Error 403' in zd.META['errors'][0] and 'dane fiskalne' in zd.META['errors'][0])
        # oba zawodzą = wyjątek z oboma powodami (main zostawia poprzedni plik)
        zd.META['errors'].clear()
        get_json, calls = self._get(fd=ValueError('brak aukcji z wynikami'), td={t: ValueError('x') for t in zd.AUK_TD_TYPES})
        with mock.patch.object(zd, 'get_json', side_effect=get_json), self.assertRaises(RuntimeError) as cm:
            zd.build_aukcje(None, now=self.NOW)
        self.assertIn('dane fiskalne: brak aukcji z wynikami', str(cm.exception)); self.assertIn('serwis aukcyjny Bill: x', str(cm.exception)); self.assertEqual(zd.META['errors'], [])
        # limit czasu zapasu: po AUK_BUDGET_S bez kolejnych zapytań
        get_json, calls = self._get(fd=ValueError('x'), td=td)
        with mock.patch.object(zd, 'get_json', side_effect=get_json), mock.patch.object(zd.time, 'monotonic', side_effect=[0, 0, 1000, 1000, 1000, 1000, 1000, 1000]):
            o = zd.build_aukcje(None, now=self.NOW)
        self.assertEqual(len(calls), 2, 'po przekroczeniu budżetu czasu tylko pierwszy rodzaj'); self.assertIn('zapas bez rodzajów: Note, Bond, TIPS, FRN', o['notes'])

    def test_main_co_6h_ponowienie_i_awaria(self):
        saved = {}
        stubs = [mock.patch.object(zd, n, side_effect=RuntimeError('offline')) for n in dir(zd) if n.startswith('build_') and n != 'build_aukcje' and callable(getattr(zd, n))]
        env = {k: '' for k in ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY', 'SITE_URL', 'CACHE_DIR')}
        young = {'at': _iso(10), 'ok': {'last': True, 'med12m': True, 'full': True}, 'last': [{'date': '2026-09-24'}]}
        old = {'at': _iso(7 * 60), 'ok': {'last': True, 'med12m': True, 'full': True}, 'last': [{'date': '2026-09-24'}]}
        zapas_young = {'at': _iso(10), 'ok': {'last': True, 'med12m': True, 'full': False}, 'last': [{'date': '2026-09-24'}]}
        zapas_old = {'at': _iso(70), 'ok': {'last': True, 'med12m': True, 'full': False}, 'last': [{'date': '2026-09-24'}]}
        built = {'at': zd.NOW, 'ok': {'last': True, 'med12m': True, 'full': True}, 'last': [{'date': '2026-09-25'}]}
        [p.start() for p in stubs]
        try:
            def run(prev, build):
                calls = []
                with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(zd, 'save', lambda n, o: saved.__setitem__(n, o)), \
                     mock.patch.object(zd, 'previous', lambda name: prev if name == 'aukcje' else None), \
                     mock.patch.object(zd, 'build_aukcje', side_effect=(lambda p, now=None: calls.append(p) or build) if not isinstance(build, Exception) else build):
                    zd.main()
                return calls
            self.assertEqual(run(young, AssertionError('nie powinien budować')), []); self.assertIs(saved['aukcje'], young); self.assertEqual(zd.META['ok']['aukcje'], 'cached')
            self.assertEqual(run(old, built), [old], 'plik sprzed 7 h — budowa (z poprzednim jako wejście)'); self.assertIs(saved['aukcje'], built); self.assertIs(zd.META['ok']['aukcje'], True)
            self.assertEqual(run(zapas_young, AssertionError('nie powinien budować')), []); self.assertEqual(zd.META['ok']['aukcje'], 'cached', 'zbudowany z zapasu 10 min temu — jeszcze z pamięci')
            self.assertEqual(run(zapas_old, built), [zapas_old], 'zbudowany z zapasu 70 min temu — ponowna próba źródła głównego')
            saved.clear()
            run(old, RuntimeError('offline')); self.assertIs(saved['aukcje'], old, 'awaria budowy — zostaje poprzedni plik'); self.assertIs(zd.META['ok']['aukcje'], False); self.assertIn('Aukcje: offline', zd.META['errors'])
            saved.clear()
            run(None, RuntimeError('offline')); self.assertNotIn('aukcje', saved, 'bez poprzedniego pliku i bez danych — nic nie zapisujemy (żadnej pustki)'); self.assertIs(zd.META['ok']['aukcje'], False)
            run(None, built); self.assertIs(saved['aukcje'], built)
        finally:
            [p.stop() for p in stubs]
        src = open(zd.__file__, encoding='utf-8').read()
        self.assertIn("prev_au = previous('aukcje')", src); self.assertIn("META['errors'].append(mask(f'Aukcje: {e}')); META['ok']['aukcje'] = False", src)
        self.assertIn("if prev_au: save('aukcje', prev_au)", src)
        me = open(__file__, encoding='utf-8').read(); a = me.index('class AukcjeV121(')
        b = min([i for i in (me.find('\nclass ', a + 1), me.find('\nif __name__', a + 1), me.find('\ndef ', a + 1)) if i > 0] or [len(me)])
        tups = [m.group(0) for m in _re_v106.finditer(r"\('build_\w+'(?:,\s*'build_\w+')+\)", me[:a] + me[b:]) if "'build_wieloryby'" in m.group(0)]
        self.assertTrue(len(tups) >= 18 and all("'build_aukcje'" in x for x in tups), 'każda lista zaślepek przebiegu głównego (z build_wieloryby) zna build_aukcje')
        self.assertEqual((zd.AUK_EVERY, zd.AUK_RETRY, zd.AUK_LAST, zd.AUK_MED_DAYS, zd.AUK_MED_MIN), (360, 60, 40, 365, 3))
        self.assertNotIn('KEY', src[src.index('def build_aukcje('):src.index('def main():')], 'źródło bez klucza')

    def test_kontrola_i_strona(self):
        root = os.path.dirname(os.path.abspath(__file__))
        k = open(os.path.join(root, 'narzedzia', 'kontrola.py'), encoding='utf-8').read()
        PL = eval(_re_v106.search(r'^PLIKI = (\[.*\])$', k, _re_v106.M).group(1)); LM = eval(_re_v106.search(r'^LIMIT_MIN = (\{[^}]*\})', k, _re_v106.M).group(1))
        self.assertIn('aukcje', PL); self.assertEqual(LM['aukcje'], 24 * 60, 'plik co 6 h: dzień bez nowego pliku = uwaga w kontroli')
        h = open(os.path.join(root, 'index.html'), encoding='utf-8').read()
        self.assertEqual(h.count('<section class="panel pcard" id="g-aukcje" hidden></section>'), 1); self.assertIn("srvJSON('aukcje')", h); self.assertIn('const EXTRA114=', h)
        wf = open(os.path.join(root, '.github', 'workflows', 'strona.yml'), encoding='utf-8').read()
        self.assertNotIn('AUKCJE', wf, 'żadnego sekretu — źródło publiczne')
