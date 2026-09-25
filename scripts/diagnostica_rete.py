#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostica di rete dell'ambiente — quali fonti sono raggiungibili DA QUI, e in quanto tempo.

PERCHE'
-------
Il plugin gira dentro la VM di Cowork, che fa passare tutto il traffico in uscita da un proxy
con allowlist di domini. Dal settembre 2026 diversi utenti segnalano che il proxy rifiuta ogni
host fuori da una lista fissa (HTTP 403 alla CONNECT), a prescindere dall'impostazione
«Consenti egress: tutti i domini». Se succede, `fonti_fetch.py` finisce in VAI_LIVE per ogni
norma non locale, WebFetch fallisce, e le lenti degradano al browser (20-60 s per pagina).

Questo script NON decide niente e non tocca niente: prova a raggiungere le fonti che il plugin
usa e stampa, per ciascuna, raggiungibile/bloccata e la latenza. Serve per due cose:
  1. capire una volta, a mano, com'e' l'ambiente (`python3 scripts/diagnostica_rete.py`);
  2. in prospettiva, farlo decidere a `round_prepara.py` in un secondo, invece di lasciare che
     ogni lente scopra il blocco a colpi di timeout.

Solo stdlib. Exit 0 sempre (e' una sonda, non un gate). `--json` per l'output macchina.

Uso:
  python3 scripts/diagnostica_rete.py
  python3 scripts/diagnostica_rete.py --json
  python3 scripts/diagnostica_rete.py --timeout 5
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
if sys.platform == "win32":  # Cowork/Desktop su Windows: le pipe sono cp1252 → UTF-8 (accenti, frecce, emoji)
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (StudioCoccolo diagnostica_rete)"

#: (etichetta, URL, metodo, corpo JSON o None). GET leggeri, o POST minimi dove la fonte
#: risponde solo a POST (API Open Data di normattiva). Ogni voce e' una fonte che qualche
#: parte del plugin usa davvero.
SONDE = [
    ("normattiva.it (permalink URN, HTML)",
     "https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:regio.decreto:1940-10-28;1443:1~art163", "GET", None),
    ("api.normattiva.it (Open Data, JSON, POST)",
     "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1/atto/dettaglio-atto-urn", "POST",
     {"urn": "urn:nir:stato:regio.decreto:1940-10-28;1443:1~art163"}),
    ("italgiure SentenzeWeb (Solr JSON)",
     "https://www.italgiure.giustizia.it/sncass/isapi/hc.dll/sn.solr/sn-collection/select?q=id:snciv2022624825O&wt=json&rows=1&fl=id",
     "GET", None),
    ("gazzettaufficiale.it", "https://www.gazzettaufficiale.it/", "GET", None),
    ("cortecostituzionale.it", "https://www.cortecostituzionale.it/ultimo-deposito", "GET", None),
    ("eur-lex CELLAR", "https://publications.europa.eu/resource/celex/32016R0679", "GET", None),
    ("garanteprivacy.it", "https://www.garanteprivacy.it/", "GET", None),
    ("brocardi.it (secondaria)", "https://www.brocardi.it/", "GET", None),
    ("github.com (riferimento: nella allowlist di default)", "https://github.com/", "GET", None),
    ("pypi.org (riferimento: nella allowlist di default)", "https://pypi.org/simple/", "GET", None),
]


def sonda(url: str, metodo: str, corpo, timeout: float) -> dict:
    """Un tentativo, un verdetto. Distingue il 403 del proxy (allowlist) da quello del sito."""
    t0 = time.monotonic()
    dati = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = Request(url, data=dati, method=metodo,
                  headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
                           **({"Content-Type": "application/json"} if dati else {})})
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=timeout, context=ctx) as r:
            r.read(2048)
            return {"esito": "OK", "http": r.status, "ms": int((time.monotonic() - t0) * 1000)}
    except HTTPError as e:
        ms = int((time.monotonic() - t0) * 1000)
        hdr = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        proxy = hdr.get("x-proxy-error") or ("proxy" in (hdr.get("server") or "").lower())
        if e.code == 403 and proxy:
            return {"esito": "BLOCCATO_PROXY", "http": 403, "ms": ms, "dettaglio": str(hdr.get("x-proxy-error"))}
        # 403/405/4xx del SITO: la rete c'e', e' la richiesta che non gli piace.
        return {"esito": "OK_RETE" if e.code < 500 else "ERRORE_SITO", "http": e.code, "ms": ms}
    except URLError as e:
        ms = int((time.monotonic() - t0) * 1000)
        motivo = str(e.reason)
        if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in motivo:
            # Catena di certificati incompleta lato sito (misurato: italgiure). La rete c'e':
            # si riprova senza verifica SOLO per dirlo, non per fidarsi del contenuto.
            try:
                with urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as r:
                    r.read(2048)
                    return {"esito": "OK_CERT_INCOMPLETO", "http": r.status,
                            "ms": int((time.monotonic() - t0) * 1000),
                            "dettaglio": "raggiungibile, ma la catena TLS del sito non si verifica da qui"}
            except Exception:
                pass
        if "403" in motivo or "CONNECT" in motivo.upper() or "Tunnel" in motivo:
            return {"esito": "BLOCCATO_PROXY", "ms": ms, "dettaglio": motivo}
        if isinstance(e.reason, socket.gaierror) or "getaddrinfo" in motivo or "Name or service" in motivo:
            return {"esito": "DNS_NEGATO", "ms": ms, "dettaglio": motivo}
        if isinstance(e.reason, socket.timeout) or "timed out" in motivo:
            return {"esito": "TIMEOUT", "ms": ms}
        return {"esito": "ERRORE_RETE", "ms": ms, "dettaglio": motivo}
    except (socket.timeout, TimeoutError):
        return {"esito": "TIMEOUT", "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # pragma: no cover - sonda: qualunque cosa e' un esito, non un crash
        return {"esito": "ERRORE", "ms": int((time.monotonic() - t0) * 1000), "dettaglio": repr(e)[:200]}


def diagnosi(esiti: list) -> str:
    fonti = [e for e in esiti if "riferimento" not in e["fonte"]]
    rif = [e for e in esiti if "riferimento" in e["fonte"]]
    ok_fonti = sum(1 for e in fonti if e["esito"] in ("OK", "OK_RETE", "OK_CERT_INCOMPLETO"))
    ok_rif = sum(1 for e in rif if e["esito"] in ("OK", "OK_RETE", "OK_CERT_INCOMPLETO"))
    bloccate = sum(1 for e in fonti if e["esito"] in ("BLOCCATO_PROXY", "DNS_NEGATO"))
    if ok_fonti == len(fonti):
        return "RETE_APERTA: tutte le fonti raggiungibili. La scala di accesso funziona come scritta (pool -> fonti_fetch -> WebFetch -> browser)."
    if ok_fonti == 0 and ok_rif > 0:
        return ("ALLOWLIST_STRETTA: i domini di riferimento passano, le fonti giuridiche no. E' il quadro delle "
                "issue Cowork di settembre 2026: fonti_fetch/WebFetch degradano tutti a VAI_LIVE. Restano "
                "utilizzabili: codici locali, WebSearch (lato server), browser (sull'host).")
    if ok_fonti == 0:
        return "OFFLINE: nessun host raggiungibile (nemmeno quelli di riferimento). Solo codici locali e cache."
    return (f"RETE_PARZIALE: {ok_fonti}/{len(fonti)} fonti raggiungibili, {bloccate} bloccate dal proxy/DNS. "
            "Guardare la tabella: le lenti vanno instradate fonte per fonte.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sonda di raggiungibilita' delle fonti giuridiche da questo ambiente.")
    ap.add_argument("--timeout", type=float, default=6.0, help="secondi per sonda (default 6)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    a = ap.parse_args(argv)

    esiti = []
    for etichetta, url, metodo, corpo in SONDE:
        r = sonda(url, metodo, corpo, a.timeout)
        r.update({"fonte": etichetta, "url": url})
        esiti.append(r)
        if not a.json:
            det = f"  ({r.get('dettaglio')})" if r.get("dettaglio") else ""
            http = f" HTTP {r['http']}" if r.get("http") else ""
            print(f"{r['esito']:<20} {r['ms']:>6} ms{http:<10} {etichetta}{det}")
    d = diagnosi(esiti)
    if a.json:
        print(json.dumps({"diagnosi": d, "sonde": esiti}, ensure_ascii=False, indent=2))
    else:
        print("\n" + d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
