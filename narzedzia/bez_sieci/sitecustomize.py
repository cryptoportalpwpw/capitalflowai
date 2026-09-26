"""Blokada sieci dla testów (v122). Włączana w kroku testów strona.yml przez PYTHONPATH=narzedzia/bez_sieci.
Każda próba połączenia (socket.connect) albo zapytania DNS z testu jest odrzucana i zapisywana; na końcu procesu, gdy były takie próby,
kod wyjścia = 3 — budowa strony staje. Powód: 26.09.2026 test harmonogramu dźwigni wołał prawdziwego budowniczego nowego źródła
(brak zaślepki), a testy i tak przechodziły, bo main() łapie wyjątki — w GitHub Actions test łączyłby się z serwerem.
Testy nigdy nie potrzebują sieci: wszystkie odpowiedzi są nagrane albo zaślepione."""
import atexit
import os
import socket
import sys
import traceback

_PROBY = []


def _gdzie():
    st = [f for f in traceback.extract_stack() if f.filename.endswith(('test_zbieraj_dane.py', 'zbieraj_dane.py', 'archiwum.py', 'kontrola.py'))]
    return ' > '.join(f'{os.path.basename(f.filename)}:{f.name}:{f.lineno}' for f in st[-5:]) or '?'


def _connect(self, *a, **k):
    _PROBY.append(_gdzie())
    raise OSError('sieć zablokowana w testach (narzedzia/bez_sieci)')


def _dns(host, *a, **k):
    _PROBY.append(f'DNS {host} via {_gdzie()}')
    raise OSError('sieć zablokowana w testach (narzedzia/bez_sieci)')


socket.socket.connect = _connect
socket.socket.connect_ex = lambda self, *a, **k: _connect(self, *a, **k)
socket.getaddrinfo = _dns
socket.create_connection = lambda *a, **k: _connect(None, *a, **k)


@atexit.register
def _koniec():
    if _PROBY:
        wiersze = sorted(set(_PROBY))
        sys.stderr.write(f'\n::error title=testy bez sieci::{len(_PROBY)} prób połączenia w testach — test woła prawdziwe źródło zamiast zaślepki\n')
        for w in wiersze[:15]:
            sys.stderr.write('  ' + w + '\n')
        sys.stderr.flush()
        os._exit(3)
