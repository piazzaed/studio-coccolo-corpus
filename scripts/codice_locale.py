#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codici locali datati — il diritto stabile viaggia col plugin (solo stdlib).

PERCHE'
-------
Due profilazioni indipendenti (11/08 e 22/08/2026) hanno misurato la stessa cosa: le lenti
spendono la maggior parte del loro tempo in I/O di rete verso normattiva, su articoli che non
cambiano da anni. L5 ha fatto 85 chiamate in 16 minuti per leggere norme del c.p.c.; il 46%
di tutte le chiamate a strumento di una pratica erano fetch normativi.

Questo modulo scarica UNA VOLTA l'intero codice da normattiva — il servizio Akoma Ntoso
ufficiale (`caricaAKN`) restituisce tutto l'atto multivigente in un solo download da ~5 MB —
e lo trasforma in file .md grep-abili, uno per libro, piu' un indice JSON. Da quel momento
`fonti_fetch.py` risponde in millisecondi dal disco invece che in secondi dalla rete, e il
plugin funziona anche offline.

COSA NON CAMBIA (il pavimento)
------------------------------
* Il testo E' normattiva: stesso servizio, stessa fonte primaria, letterale. Cambia il
  trasporto, non la fonte. Ogni risposta dichiara la data dello snapshot e la data di
  vigenza dell'articolo.
* Le claim DATATE restano rigorose: l'AKN porta, per OGNI articolo, la data della sua
  versione vigente (`vigore_da`). Il locale risponde su una data-evento SOLO se
  data-evento >= vigore_da (il testo alla data coincide col vigente). Altrimenti si va su
  normattiva live con l'URN datato, come sempre — tempus regit actum non si negozia.
* La giurisprudenza resta SEMPRE live (nucleo §3-ter): qui ci sono solo codici.
* Uno snapshot vecchio NON tace piu' (v0.29): oltre la vita massima (45 giorni dall'ultima
  verifica) il locale risponde lo stesso ma con `snapshot_oltre_vita: true` e la riga
  «SNAPSHOT NON CONFERMATO dal AAAA-MM-GG» — su Cowork, con la rete bloccata, il silenzio
  lasciava l'avvocato senza testo; ora il Livello 0 tratta le claim fondanti su quel testo come
  residue. `corpus_refresh.py` (Fase 4) lo rinfresca ogni 7 giorni (scheduled task settimanale +
  fallback all'avvio della prima pratica), `corpus_diff.py` scrive le differenze in
  `changelog-auto.md`, `storico.py` serve i testi anteriori alla versione vigente dalla cache
  permanente (confini di versione in `_meta.versioni`).
* DATAZIONE PER ARTICOLO (v0.29). I codici (c.c., c.p.c., c.p., disp. att.) portano nell'AKN la
  data della versione di OGNI articolo (FRBRWork). Gli atti «monoblocco» (leggi speciali, TUB,
  CAP, CCII…) no: fino alla v0.28 la data si stimava dai rimandi di nota, e il 23/09/2026 il
  confronto con l'API Open Data di normattiva (`articoloDataInizioVigenza`) l'ha smentita nella
  direzione PERICOLOSA (data stimata anteriore a quella vera) sulla maggioranza del campione:
  art. 125-bis TUB stimato 2024-12-27, vero 2026-01-10; CCII «originale» 2019-02-14, in vigore
  dal 2022-07-15. Ora la data di questi articoli viene SOLO dall'API (validata sul testo); se
  l'API non risponde l'articolo resta senza data (`ignota`) e vale la regola prudente del
  consolidamento. La stima euristica non entra piu' nel gate.

DOVE VIVE
---------
* Seed spedito col bundle:   wiki-studio/normativa/testi/   (rigenerato a ogni rilascio)
* Refresh a runtime:         <stato_root>/testi/            (la Sentinella scrive qui:
  fuori dal pacchetto, sopravvive alla reinstallazione — stessa logica del corpus vivo)
Il lookup prova prima il refresh runtime, poi il seed del bundle.

CLI:
  python3 scripts/codice_locale.py --costruisci cpc [--nel-repo] [--da-xml file.xml] [--senza-datazione]
  python3 scripts/codice_locale.py --art "163" --codice cpc [--data-evento 2024-01-14]
  python3 scripts/codice_locale.py --data-api tub [--nel-repo]    # (ri)data gli articoli di un indice esistente
  python3 scripts/codice_locale.py --stato [--json]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
import time
if sys.platform == "win32":  # Cowork/Desktop su Windows: le pipe sono cp1252 → UTF-8 (accenti, frecce, emoji)
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
import unicodedata
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import WIKI, stato_root  # noqa: E402

#: Vita MASSIMA dello snapshot, in giorni: oltre, il locale smette di rispondere e si torna
#: alla rete. 45 giorni copre piu' cicli di refresh mancati (Cowork con egress bloccato) senza
#: spegnere il corpus alla prima settimana storta. Override da codici.json (`vita_massima_giorni`).
VITA_UTILE_GIORNI = 45

#: Vita di REFRESH (piano v0.25, D5.2): oltre questi giorni `corpus_refresh.py` riscarica l'atto e la
#: cascata «vale oggi?» del Livello 0 segnala lo snapshot come non piu' fresco. Da codici.json
#: (`vita_utile_giorni`); 7 = il ciclo settimanale dello scheduled task.
VITA_REFRESH_GIORNI = 7

#: Codici costruibili. Per ciascuno: URN caricaAKN (dataGU, codiceRedaz) e — per gli atti
#: divisi in libri — i confini noti, stabili dalla promulgazione, usati solo per spezzare
#: i file in pezzi leggibili.
CODICI = {
    "cpc": {
        "nome": "Codice di procedura civile",
        "doc_prefix": "CODICE DI PROCEDURA CIVILE",
        "min_articoli": 900,
        "urn": "urn:nir:stato:regio.decreto:1940-10-28;1443:1",
        "libri": [("Libro I — Disposizioni generali", 1, 162),
                  ("Libro II — Del processo di cognizione", 163, 473),
                  ("Libro III — Del processo di esecuzione", 474, 632),
                  ("Libro IV — Dei procedimenti speciali", 633, 840)],
    },
    "cc": {
        "nome": "Codice civile",
        # Il R.D. 262/1942 contiene ANCHE le preleggi, coi loro artt. 1-31: senza questo
        # filtro l'art. 1 c.c. usciva con la rubrica del codice e il testo delle preleggi.
        "doc_prefix": "CODICE CIVILE",
        "min_articoli": 2900,
        "urn": "urn:nir:stato:regio.decreto:1942-03-16;262:2",
        "libri": [("Libro I — Delle persone e della famiglia", 1, 455),
                  ("Libro II — Delle successioni", 456, 809),
                  ("Libro III — Della proprietà", 810, 1172),
                  ("Libro IV — Delle obbligazioni", 1173, 2059),
                  ("Libro V — Del lavoro", 2060, 2642),
                  ("Libro VI — Della tutela dei diritti", 2643, 2969)],
    },
    "disp-att-cpc": {
        "nome": "Disposizioni di attuazione del c.p.c.",
        "doc_prefix": "",
        "min_articoli": 250,          # atto monoblocco: nessuna serie concorrente
        "urn": "urn:nir:stato:regio.decreto:1941-12-18;1368:1",
        "libri": [("Disposizioni di attuazione", 1, 999)],
    },
    "preleggi": {
        "nome": "Disposizioni sulla legge in generale (preleggi)",
        "doc_prefix": "DISPOSIZIONI SULLA LEGGE IN GENERALE",
        "min_articoli": 25,
        "urn": "urn:nir:stato:regio.decreto:1942-03-16;262:2",
        # l'AKN e' quello del c.c. (un solo download), ma il permalink per articolo e' «:1»
        "urn_permalink": "urn:nir:stato:regio.decreto:1942-03-16;262:1",
        "libri": [("Disposizioni sulla legge in generale", 1, 31)],
    },
    "cost": {
        "nome": "Costituzione della Repubblica Italiana",
        "doc_prefix": "",
        "min_articoli": 130,
        "urn": "urn:nir:stato:costituzione:1947-12-27",
        "libri": [("Costituzione", 1, 139)],
    },
    # --- v0.23: leggi speciali che le pratiche dello Studio aprono di piu' (piano, fase 7).
    #     Atti monoblocco: un solo file, nessuna serie concorrente. `min_articoli` e' un
    #     sanity check sul download (un AKN troncato non deve sostituire un seed buono).
    "cds": {
        "nome": "Codice della strada (D.Lgs. 285/1992)",
        "doc_prefix": "", "min_articoli": 200,
        "urn": "urn:nir:stato:decreto.legislativo:1992-04-30;285",
        "libri": [("Codice della strada", 1, 999)],
    },
    "reg-cds": {
        "nome": "Regolamento di esecuzione del Codice della strada (D.P.R. 495/1992)",
        "doc_prefix": "", "min_articoli": 300,
        "urn": "urn:nir:stato:decreto.del.presidente.della.repubblica:1992-12-16;495",
        "libri": [("Regolamento CdS", 1, 999)],
    },
    "cap": {
        "nome": "Codice delle assicurazioni private (D.Lgs. 209/2005)",
        "doc_prefix": "", "min_articoli": 250,
        "urn": "urn:nir:stato:decreto.legislativo:2005-09-07;209",
        "libri": [("Codice delle assicurazioni", 1, 999)],
    },
    "dlgs-28-2010": {
        "nome": "Mediazione civile e commerciale (D.Lgs. 28/2010)",
        "doc_prefix": "", "min_articoli": 15,
        "urn": "urn:nir:stato:decreto.legislativo:2010-03-04;28",
        "libri": [("D.Lgs. 28/2010", 1, 999)],
    },
    "dl-132-2014": {
        "nome": "Negoziazione assistita e degiurisdizionalizzazione (D.L. 132/2014)",
        "doc_prefix": "", "min_articoli": 15,
        "urn": "urn:nir:stato:decreto.legge:2014-09-12;132",
        "libri": [("D.L. 132/2014", 1, 999)],
    },
    "l-241-1990": {
        "nome": "Procedimento amministrativo (L. 241/1990)",
        "doc_prefix": "", "min_articoli": 25,
        "urn": "urn:nir:stato:legge:1990-08-07;241",
        "libri": [("L. 241/1990", 1, 999)],
    },
    "tuel": {
        "nome": "Testo unico degli enti locali (D.Lgs. 267/2000)",
        "doc_prefix": "", "min_articoli": 250,
        "urn": "urn:nir:stato:decreto.legislativo:2000-08-18;267",
        "libri": [("TUEL", 1, 999)],
    },
    # --- v0.24: strato 3 del corpus successioni (ARCHITETTURA-SUCCESSIONI §3). Nella pratica
    #     Properzi (16/9/2026) cinque agenti hanno tentato normattiva per gli artt. 2 e 10 del
    #     TU 347/1990 e l'URN del decreto restituiva il preambolo: i testi unici approvati con
    #     decreto stanno nell'ALLEGATO, che l'AKN porta per intero. Da qui in poi i testi
    #     vigenti viaggiano col plugin e il round su una successione non apre la rete.
    "tus": {
        "nome": "Testo unico imposta sulle successioni e donazioni (D.Lgs. 346/1990)",
        "doc_prefix": "", "min_articoli": 55,
        "urn": "urn:nir:stato:decreto.legislativo:1990-10-31;346",
        "libri": [("TUS", 1, 999)],
    },
    "tu-347-1990": {
        "nome": "Testo unico imposte ipotecaria e catastale (D.Lgs. 347/1990)",
        "doc_prefix": "", "min_articoli": 15,
        "urn": "urn:nir:stato:decreto.legislativo:1990-10-31;347",
        "libri": [("TU 347/1990", 1, 999)],
    },
    "dpr-131-1986": {
        "nome": "Testo unico imposta di registro (D.P.R. 131/1986)",
        # Misurato il 18/9/2026: gli <attachment><doc> dell'AKN sono SOLO Tariffa e Tabella
        # (39 articoli che collidono coi numeri del testo unico: l'art. 52 sul valore
        # catastale spariva). Gli articoli del testo unico stanno nel <body> principale.
        "doc_prefix": "", "min_articoli": 60, "solo_corpo": True,
        "urn": "urn:nir:stato:decreto.del.presidente.della.repubblica:1986-04-26;131",
        "libri": [("TU registro", 1, 999)],
    },
    "dl-262-2006": {
        "nome": "D.L. 262/2006 (art. 2 c. 47-53: reintroduzione dell'imposta di successione)",
        "doc_prefix": "", "min_articoli": 20,
        "urn": "urn:nir:stato:decreto.legge:2006-10-03;262",
        "libri": [("D.L. 262/2006", 1, 999)],
    },
    "dlgs-139-2024": {
        "nome": "Riforma imposte di successione, donazione e registro (D.Lgs. 139/2024)",
        "doc_prefix": "", "min_articoli": 5,
        "urn": "urn:nir:stato:decreto.legislativo:2024-09-18;139",
        "libri": [("D.Lgs. 139/2024", 1, 999)],
    },
    "l-104-1992": {
        "nome": "Legge-quadro sull'handicap (L. 104/1992)",
        "doc_prefix": "", "min_articoli": 35,
        "urn": "urn:nir:stato:legge:1992-02-05;104",
        "libri": [("L. 104/1992", 1, 999)],
    },
    "dpr-445-2000": {
        "nome": "Testo unico documentazione amministrativa (D.P.R. 445/2000)",
        "doc_prefix": "", "min_articoli": 60,
        "urn": "urn:nir:stato:decreto.del.presidente.della.repubblica:2000-12-28;445",
        "libri": [("DPR 445/2000", 1, 999)],
    },
}

#: Come fonti_fetch normalizza i codici -> slug locale (le descrizioni degli atti numerati
#: escono da parse_riferimento come "<alias> <numero>/<anno>", minuscole).
ALIAS = {"c.p.c.": "cpc", "c.c.": "cc", "disp. att. c.p.c.": "disp-att-cpc",
         "cost.": "cost", "preleggi": "preleggi",
         "d.lgs. 285/1992": "cds", "dlgs 285/1992": "cds", "d. lgs. 285/1992": "cds",
         "d.p.r. 495/1992": "reg-cds", "dpr 495/1992": "reg-cds",
         "d.lgs. 209/2005": "cap", "dlgs 209/2005": "cap",
         "d.lgs. 28/2010": "dlgs-28-2010", "dlgs 28/2010": "dlgs-28-2010",
         "d.l. 132/2014": "dl-132-2014", "dl 132/2014": "dl-132-2014",
         "l. 241/1990": "l-241-1990", "legge 241/1990": "l-241-1990",
         "d.lgs. 267/2000": "tuel", "dlgs 267/2000": "tuel",
         # v0.24 — corpus successioni (strato 3)
         "d.lgs. 346/1990": "tus", "dlgs 346/1990": "tus", "d. lgs. 346/1990": "tus", "tus": "tus",
         "d.lgs. 347/1990": "tu-347-1990", "dlgs 347/1990": "tu-347-1990", "d. lgs. 347/1990": "tu-347-1990",
         "d.p.r. 131/1986": "dpr-131-1986", "dpr 131/1986": "dpr-131-1986",
         "d.l. 262/2006": "dl-262-2006", "dl 262/2006": "dl-262-2006",
         "d.lgs. 139/2024": "dlgs-139-2024", "dlgs 139/2024": "dlgs-139-2024",
         "l. 104/1992": "l-104-1992", "legge 104/1992": "l-104-1992",
         "d.p.r. 445/2000": "dpr-445-2000", "dpr 445/2000": "dpr-445-2000"}


#: Versione del FORMATO dell'indice/.md. 2 (v0.29): numeri dei commi conservati negli atti monoblocco,
#: rubriche su una riga, data per articolo dall'API. Un indice di formato piu' vecchio (refresh runtime
#: fatto dal plugin precedente) non vince sul seed del formato corrente, e corpus_diff non confronta
#: formati diversi (le differenze sarebbero di forma: ~6.000 falsi «modificato» nel changelog-auto).
FORMATO_INDICE = 2

#: Materie servite da ciascun atto (da codici.json): usate dal seed del ledger e dal Livello 0.
MATERIE: dict = {}

#: Come si cita l'atto in un riferimento che fonti_fetch sa risolvere («c.c.», «TUB», «D.Lgs. 28/2010»):
#: da codici.json (`citazione`) o dedotto dall'URN. Lo usa corpus_cerca.py per il comando pronto.
CITAZIONI: dict = {}

#: Sigle canoniche dei tipi di atto, nella forma in cui fonti_fetch.parse_riferimento scrive la
#: descrizione («d.lgs. 28/2010»): gli alias canonici di ogni atto numerato si generano dall'URN,
#: cosi' «decreto legislativo 28/2010», «L. 431/98» e «dlgs 28/2010» arrivano tutti allo stesso slug.
_SIGLE_TIPO_URN = {
    "decreto.legislativo": ("d.lgs.", "dlgs", "d. lgs.", "D.Lgs."),
    "legge": ("l.", "legge", "L."),
    "decreto.legge": ("d.l.", "dl", "D.L."),
    "decreto.del.presidente.della.repubblica": ("d.p.r.", "dpr", "D.P.R."),
    "regio.decreto": ("r.d.", "rd", "R.D."),
    "decreto": ("d.m.", "dm", "D.M."),
}


def estremi_urn(urn: str):
    """(tipo, numero, anno) da un URN normattiva («urn:nir:stato:decreto.legislativo:2010-03-04;28» →
    ('decreto.legislativo', '28', '2010')), con o senza data completa e suffisso di allegato; None se non numerato."""
    m = re.match(r"urn:nir:[^:]+:([a-z.]+):(\d{4})(?:-\d{2}-\d{2})?;(\d+)", str(urn or ""))
    if not m:
        return None
    return m.group(1), str(int(m.group(3))), m.group(2)


def _alias_da_urn() -> None:
    """Alias canonici generati dall'URN per ogni atto numerato del corpus (mai per gli URN condivisi:
    R.D. 262/1942 = preleggi + c.c., che si citano per sigla). Non sovrascrive alias espliciti."""
    per_chiave = {}
    for slug, cfg in CODICI.items():
        e = estremi_urn(cfg.get("urn"))
        if e:
            per_chiave.setdefault(e, []).append(slug)
    for (tipo, numero, anno), slugs in per_chiave.items():
        if len(slugs) != 1:
            continue
        for sigla in _SIGLE_TIPO_URN.get(tipo, ())[:-1]:
            ALIAS.setdefault(f"{sigla} {numero}/{anno}", slugs[0])


def slug_da_riferimento(descr: str, urn_base: str = "") -> str:
    """Lo slug del corpus per la descrizione di fonti_fetch.parse_riferimento («d.lgs. 28/2010», «c.c.»,
    «preleggi»), con ripiego sugli estremi dell'URN (tipo, numero, anno) per le forme non censite. '' se
    l'atto non e' nel corpus. L'URN condiviso (R.D. 262/1942: preleggi + c.c.) non decide mai da solo."""
    slug = ALIAS.get(str(descr or "").lower())
    if slug:
        return slug
    e = estremi_urn(urn_base)
    if not e:
        return ""
    trovati = [s for s, cfg in CODICI.items() if estremi_urn(cfg.get("urn")) == e]
    return trovati[0] if len(trovati) == 1 else ""


def urn_permalink(slug: str) -> str:
    """L'URN su cui costruire il permalink normattiva di un articolo dell'atto («…~artN!vig=…»)."""
    cfg = CODICI.get(slug) or {}
    return str(cfg.get("urn_permalink") or cfg.get("urn") or "")


def citazione(slug: str) -> str:
    """La forma breve con cui citare l'atto in un riferimento risolvibile («c.c.», «D.Lgs. 28/2010»)."""
    if CITAZIONI.get(slug):
        return CITAZIONI[slug]
    e = estremi_urn((CODICI.get(slug) or {}).get("urn"))
    if e and e[0] in _SIGLE_TIPO_URN:
        return f"{_SIGLE_TIPO_URN[e[0]][-1]} {e[1]}/{e[2]}"
    return slug


def _carica_codici_json() -> dict:
    """v0.25 (piano velocita', D5.1): l'elenco degli atti e' CONFIG-DRIVEN.

    `wiki-studio/normativa/codici.json` e' la fonte; il dizionario Python sopra resta come
    fallback se il file manca o e' illeggibile (mai rompere il lookup per un JSON rotto).
    Le voci del JSON sovrascrivono quelle omonime e ne aggiungono di nuove; ogni `alias`
    entra nella mappa ALIAS (forma minuscola, come esce da fonti_fetch.parse_riferimento).
    """
    try:
        import corpus as _cp
        raw = _cp.risolvi("normativa/codici.json")
    except Exception:
        raw = None
    if not raw:
        f = WIKI / "normativa" / "codici.json"
        if not f.exists():
            return {}
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            return {}
    try:
        cfg = json.loads(raw)
    except ValueError:
        return {}
    atti = cfg.get("atti") if isinstance(cfg, dict) else None
    if not isinstance(atti, dict):
        return {}
    global VITA_REFRESH_GIORNI, VITA_UTILE_GIORNI
    meta_cfg = cfg.get("_meta") if isinstance(cfg.get("_meta"), dict) else {}
    try:
        if meta_cfg.get("vita_utile_giorni"):
            VITA_REFRESH_GIORNI = int(meta_cfg["vita_utile_giorni"])
        if meta_cfg.get("vita_massima_giorni"):
            VITA_UTILE_GIORNI = int(meta_cfg["vita_massima_giorni"])
    except (TypeError, ValueError):
        pass
    for slug, a in atti.items():
        if not isinstance(a, dict) or not a.get("urn"):
            continue
        voce = {
            "nome": a.get("nome") or slug,
            "doc_prefix": a.get("doc_prefix") or "",
            "min_articoli": int(a.get("min_articoli") or 5),
            "urn": a["urn"],
            "libri": [tuple(x) for x in (a.get("libri") or [[a.get("nome") or slug, 1, 9999]])],
        }
        if a.get("solo_corpo"):
            voce["solo_corpo"] = True
        if a.get("solo_allegati"):
            voce["solo_allegati"] = True
        if a.get("urn_permalink"):
            voce["urn_permalink"] = a["urn_permalink"]
        if a.get("peso_ricerca") is not None:
            voce["peso_ricerca"] = a["peso_ricerca"]
        CODICI[slug] = voce
        for al in a.get("alias") or []:
            ALIAS[str(al).lower()] = slug
        MATERIE[slug] = list(a.get("materie") or [])
        if a.get("citazione"):
            CITAZIONI[slug] = str(a["citazione"])
    return atti


def _carica_codici_remoti() -> list:
    """v0.30: atti aggiunti al corpus pubblico dopo il bundle (`<stato_root>/normativa/codici-remoto.json`,
    scaricato da corpus_sync.py). Entrano SOLO gli slug nuovi: quelli gia' noti restano come li conosce
    questo plugin (i campi di config sono codice, non dati)."""
    try:
        p = stato_root() / "normativa" / "codici-remoto.json"
        if not p.exists():
            return []
        atti = (json.loads(p.read_text(encoding="utf-8")) or {}).get("atti") or {}
    except (OSError, ValueError):
        return []
    nuovi = []
    for slug, a in atti.items():
        if slug in CODICI or not isinstance(a, dict) or not a.get("urn"):
            continue
        CODICI[slug] = {"nome": a.get("nome") or slug, "doc_prefix": a.get("doc_prefix") or "",
                        "min_articoli": int(a.get("min_articoli") or 5), "urn": a["urn"],
                        "libri": [tuple(x) for x in (a.get("libri") or [[a.get("nome") or slug, 1, 9999]])]}
        for k in ("solo_corpo", "solo_allegati", "urn_permalink", "peso_ricerca"):
            if a.get(k) is not None:
                CODICI[slug][k] = a[k]
        for al in a.get("alias") or []:
            ALIAS[str(al).lower()] = slug
        MATERIE[slug] = list(a.get("materie") or [])
        if a.get("citazione"):
            CITAZIONI[slug] = str(a["citazione"])
        nuovi.append(slug)
    return nuovi


try:
    _carica_codici_json()
except Exception:  # pragma: no cover - la config non deve mai rompere il modulo
    pass
try:
    _carica_codici_remoti()
except Exception:  # pragma: no cover
    pass
try:
    _alias_da_urn()
except Exception:  # pragma: no cover
    pass


def slug_per_materia(materia: str) -> list:
    """Slug degli atti che servono una materia (da codici.json)."""
    m = str(materia or "").strip().lower()
    return [s for s, ms in MATERIE.items() if m in ms]


def _dir_runtime() -> Path:
    return stato_root() / "testi"


def _dir_seed() -> Path:
    return WIKI / "normativa" / "testi"


def _oggi() -> _dt.date:
    return _dt.date.today()


# ============================================================ parsing AKN

def _norm_token(grezzo: str) -> str:
    """'art. 281 undecies' / 'art. 281-bis' / 'art. 840 bis.1' -> '281-undecies', '281-bis', '840-bis.1'."""
    s = grezzo.lower().replace("art.", "").strip(" .")
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    # normattiva scrive "novies" dove la Gazzetta (e gli avvocati) scrivono "nonies":
    # art. 21-nonies L. 241/1990 e' <article eId="art_21-novies"> nell'AKN.
    return s.replace("nonies", "novies")


def _pulisci(testo: str) -> str:
    t = unescape(testo)
    t = unicodedata.normalize("NFC", t)
    righe = [re.sub(r"[ \t]+", " ", r).strip() for r in t.splitlines()]
    out, vuote = [], 0
    for r in righe:
        if not r:
            vuote += 1
            if vuote > 1:
                continue
        else:
            vuote = 0
        out.append(r)
    return "\n".join(out).strip()


_RX_NOTA_CODA = re.compile(r"(?:[\s.)]*\(\(?\s*\d{1,3}\s*\)?\)?|\s[.)]+\s*\d{1,3})\s*$")


def pulisci_rubrica(grezza: str):
    """(rubrica, natura) — la rubrica su UNA riga, senza i segni editoriali di normattiva.

    Misurato il 23/09/2026 sul corpus: 1.200 rubriche «sporche» su 10.035 articoli — doppie
    parentesi di modifica «(( (Reclami) ))», parentesi esterne, marcatore di rango «(L)»/«(R)»
    dei testi unici misti (D.P.R. 445/2000, 115/2002, 380/2001), corrispondenze con la norma
    previgente «(Art. 10 Cod. Str.)», rimandi di nota «((50». Peggio: una rubrica su piu' righe
    («(L)\\nEsenzioni») spezzava l'intestazione «### Art. N — …» del .md e il lookup restituiva
    il commento <!-- vigore_da --> dentro il testo (art. 10 D.P.R. 115/2002, contributo unificato).
    `natura` e' «L»/«R» quando il testo unico lo dichiara (norma legislativa/regolamentare)."""
    s = unescape(str(grezza or ""))
    natura = ""
    righe = [r.strip() for r in s.splitlines() if r.strip()]
    tenute = []
    for i, r in enumerate(righe):
        mn = re.fullmatch(r"\(\(?\s*([LR])\s*\)?\)", r)
        if mn:
            natura = natura or mn.group(1)
            continue
        if len(righe) > 1 and re.match(r"(?i)^\(*\s*artt?\.\s*\d", r) and i < len(righe) - 1:
            continue                      # corrispondenza con la norma previgente
        if tenute and re.match(r"^\(?\(?\s*(?:\d{1,3}(?:-[a-z]+)?\.|[a-z]\))\s", r):
            break                         # e' gia' il testo del primo comma
        tenute.append(r)
    s = " ".join(tenute)
    mn = re.match(r"^\(([LR])\)\s+(.+)$", s)
    if mn:
        natura, s = natura or mn.group(1), mn.group(2)
    s = s.replace("((", " ").replace("))", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = _RX_NOTA_CODA.sub("", s).strip() if len(s) > 12 else s
    for _ in range(4):
        prima = s
        s = s.strip()
        if s.startswith("(") and s.endswith(")") and s.count("(") == s.count(")"):
            s = s[1:-1].strip()
        s = re.sub(r"\)\s*\.\s*$", ")", s)
        if re.search(r"[^\s.]\.$", s) and not re.search(r"\b[a-zA-Z]\.[a-zA-Z]{1,3}\.$", s):
            s = s[:-1].rstrip()
        if s.count("(") != s.count(")"):
            s = s.replace("(", " ").replace(")", " ")
            s = re.sub(r"\s+", " ", s).strip(" .")
        if s == prima:
            break
    return s.strip(), natura


def _separa_rubrica(testo: str):
    """(rubrica, testo) per gli articoli dei codici (doc per articolo): la rubrica e' la prima
    parentesi, anche avvolta nelle doppie parentesi di modifica. Mai oltre una riga di comma:
    il 23/09/2026 l'art. 11 TUS usciva con «1. Si considerano compresi nell'attivo ereditario: a»
    nella rubrica — la parentesi della corrispondenza non si chiudeva e la regex arrivava a «a)»."""
    rubrica = ""
    # Articolo integralmente sostituito: normattiva avvolge anche la rubrica nelle doppie
    # parentesi di modifica — "(( (Rubrica) ))". Va riconosciuta PRIMA della forma comune,
    # altrimenti la regex si ferma alla prima ")" e lascia rubriche monche e testi che
    # aprono con "))" (misurato: 114 articoli su 1056).
    rm = re.match(r"\(\(\s*\(([^()]{2,200})\)\.?\s*\)\)\s*\n?", testo)
    if not rm:
        rm = re.match(r"\(([^()]{2,200})\)\.?\s*\n?", testo)
    if rm and re.search(r"\n\s*(?:\d{1,3}(?:-[a-z]+)?\.|[a-z]\))\s", rm.group(1)):
        rm = None
    # Una vera rubrica sta su una parentesi SINGOLA. Negli abrogati la dicitura e' fra
    # DOPPIE parentesi — ((ARTICOLO ABROGATO...)) — e mangiarla come rubrica lasciava
    # l'articolo col corpo ridotto a ")". Se dopo il taglio non resta nulla, la
    # "rubrica" ERA il testo: si ripristina tutto.
    if rm and "ABROGATO" not in rm.group(1).upper():
        resto = testo[rm.end():].strip()
        if len(resto) >= 10:
            rubrica, testo = rm.group(1).strip(), resto
    # Testi unici (TUS 346/1990): la prima parentesi e' la CORRISPONDENZA con la norma
    # previgente — "(Art. 35 D.P.R. n. 637/1972)" — e la rubrica vera e' la riga dopo.
    # Misurato il 18/9/2026: art. 6 TUS usciva con rubrica "Art. 35 D.P.R. n. 637/1972".
    if re.match(r"(?i)^artt?\.\s*\d", rubrica):
        prima, _, dopo = testo.partition("\n")
        prima = prima.strip().strip("()").strip()
        if 2 <= len(prima) <= 120 and not re.match(r"^\(?\(?\s*\d", prima) and len(dopo.strip()) >= 10:
            rubrica, testo = prima, dopo.strip()
    elif not rubrica and re.match(r"(?i)^\(artt?\.\s*\d", testo):
        # corrispondenza su piu' righe, parentesi non chiusa (art. 11 TUS): le righe «Art. …»
        # sono la corrispondenza, la prima riga che non lo e' e' la rubrica
        righe = testo.split("\n")
        k = 0
        while k < len(righe) and re.match(r"(?i)^\(?\s*artt?\.\s*\d", righe[k].strip()):
            k += 1
        if k < len(righe) - 1:
            cand = righe[k].strip().strip("()").strip()
            dopo = "\n".join(righe[k + 1:]).strip()
            if 2 <= len(cand) <= 120 and not re.match(r"^\(?\(?\s*\d", cand) and len(dopo) >= 10:
                rubrica, testo = cand, dopo
    return rubrica, testo


def _articoli_monoblocco(segmento: str, eventi: dict, originale: str) -> list:
    """Gli <article eId="art_N"> di un segmento dell'AKN (atti senza doc per articolo)."""
    out = []
    # «.» nell'eId: TUB 69.1/96.1, TUF 4-quinquies.1, CAP 132.1 (il preventivatore RC auto).
    # Fino alla v0.28 la regex li scartava: 42 articoli del TUB e 60 del TUF non esistevano.
    for m in re.finditer(r"<article[^>]*eId=\"art_([0-9a-z_.-]+)\"[^>]*>(.*?)</article>", segmento, re.S):
        token = _norm_token("art. " + m.group(1).replace("_", " "))
        corpo = m.group(2)
        hm = re.search(r"<heading[^>]*>(.*?)</heading>", corpo, re.S)
        rubrica = _pulisci(re.sub(r"<[^>]+>", "", hm.group(1))) if hm else ""
        corpo_senza = re.sub(r"<heading.*?</heading>|<heading\s*/>", "", corpo, flags=re.S)
        # Il numero dell'ARTICOLO si toglie; quelli dei commi e delle lettere («1.», «2-bis.», «a)») restano,
        # attaccati al loro testo. Fino alla v0.28 si toglievano tutti: «art. 5, comma 1-bis, D.Lgs. 28/2010»
        # non era piu' individuabile e norma.py --commi contava i capoversi (le lettere diventavano commi).
        corpo_senza = re.sub(r"^\s*<num>[^<]*</num>", "", corpo_senza, count=1)
        corpo_senza = re.sub(r"<num>(.*?)</num>(?:\s*<(?!/)[^>]*>)*\s*",
                             lambda mm: re.sub(r"<[^>]+>", "", mm.group(1)).strip() + " ", corpo_senza, flags=re.S)
        testo = _pulisci(re.sub(r"<[^>]+>", "", corpo_senza))
        if not testo and len(rubrica) > 200:
            # Articolo inserito in sede di conversione: normattiva mette TUTTO l'articolo dentro
            # <heading> — «(( (Rubrica). 1. testo… ))». Misurato: art. 25-decies D.Lgs. 231/2001 (un
            # reato presupposto!) e 14 articoli del D.L. 1/2012 sparivano dal corpus.
            rm = re.match(r"^\(\(\s*\(([^()]{2,250})\)\.?\s*(.+)$", rubrica, re.S)
            if rm:
                rubrica, testo = rm.group(1).strip(), "((" + rm.group(2).strip()
            else:
                testo, rubrica = rubrica, ""
        if testo and not pulisci_rubrica(rubrica)[0]:
            # <heading> vuoto (o solo «(L)»): la rubrica sta nel testo. Misurato il 23/09/2026: 399 articoli
            # su 409 del Reg. CdS, 296 su 316 del D.P.R. 115/2002 («(Oggetto)» come primo capoverso), la
            # L. 24/2017 con la rubrica come primo paragrafo senza numero. Senza, la ricerca per argomento
            # perdeva il campo che pesa di piu'.
            r2, t2 = _separa_rubrica(testo)
            if r2:
                rubrica, testo = (rubrica + "\n" + r2) if rubrica else r2, t2
            else:
                prima, _, resto = testo.partition("\n")
                prima = prima.strip()
                if (3 <= len(prima) <= 150 and not re.match(r"^[\d(\"«]", prima) and not re.search(r"[.;:,]$", prima)
                        and re.match(r"^\s*\(?\(?\s*1\.\s", resto.strip() + " ")):
                    rubrica, testo = (rubrica + "\n" + prima) if rubrica else prima, resto.strip()
        if testo:
            vigore_da, base = data_articolo_monoblocco(corpo, eventi, originale)
            out.append({"token": token, "rubrica": rubrica, "vigore_da": vigore_da,
                        "datazione": base, "testo": testo})
    return out


def _togli_decreto_di_approvazione(articoli: list) -> list:
    """Testi unici approvati con decreto e pubblicati nello STESSO body (TUEL, D.Lgs. 267/2000): prima
    l'articolo del decreto («E' approvato l'unito testo unico…»), poi il testo unico che riparte da 1.
    Il 23/09/2026 l'art. 1 TUEL restituiva il decreto con la rubrica del testo unico. Si toglie la serie
    iniziale SOLO se e' corta (<= 3 articoli), approva un testo e la numerazione riparte."""
    visti = set()
    for i, a in enumerate(articoli):
        if a["token"] in visti:
            testa = articoli[:i]
            if 1 <= len(testa) <= 3 and any(re.search(r"(?i)\b(?:e'|è)\s+approvat[oa]\b", x["testo"]) for x in testa):
                return articoli[i:]
            return articoli
        visti.add(a["token"])
    return articoli


def parse_akn(xml: str, nome_codice: str, doc_prefix: str = "", solo_corpo: bool = False,
              solo_allegati: bool = False) -> list[dict]:
    """Estrae gli articoli dagli <attachment><doc> dell'AKN normattiva.

    Regex e non ElementTree per una ragione precisa: il file e' grande, la struttura che ci
    serve e' piatta e ripetitiva, e un namespace che cambia non deve rompere il parser.

    `solo_corpo` (v0.24): per gli atti in cui gli allegati sono tariffe e tabelle con una
    numerazione propria (DPR 131/1986), si leggono SOLO gli <article> del <body> principale
    e si ignorano gli attachment: altrimenti l'art. 1 della Tariffa sovrascrive l'art. 1
    del testo unico e gli articoli senza omonimo in tariffa (52, 53-bis…) spariscono.
    `solo_allegati` (v0.29): il contrario — il testo sta negli allegati, il body e' il decreto
    di approvazione. Numeri doppi fra le serie li rifiuta `costruisci` (mai un doppione muto).
    """
    articoli = []
    if solo_corpo:
        taglio = xml.find("<attachments")
        xml = xml[:taglio] if taglio > 0 else xml
    for m in re.finditer(r"<doc name=\"([^\"]+)\">(.*?)</doc>", xml if not solo_corpo else "", re.S):
        nome, corpo = m.group(1), m.group(2)
        # un atto puo' contenere PIU' serie di articoli (R.D. 262/1942 = preleggi + codice
        # civile, numerazioni che collidono): si tiene solo la serie richiesta.
        if doc_prefix and not nome.upper().startswith(doc_prefix.upper() + "-"):
            continue
        am = re.search(r"art\.\s*(.+)$", nome, re.I)
        if not am:
            continue
        token = _norm_token("art. " + am.group(1))
        # Data dell'ultima modifica dell'articolo: sta in FRBRWork/FRBRdate (misurato:
        # art. 281-undecies -> 2024-11-26, correttivo Cartabia; art. 1 -> 1942-04-21).
        # FRBRExpression porta invece la data dell'ATTO (1942 per tutti): usarla renderebbe
        # il gate tempus-regit-actum un colabrodo — ogni claim datata passerebbe.
        vm = re.search(r"<FRBRWork>.*?<FRBRdate date=\"(\d{4}-\d{2}-\d{2})\"", corpo, re.S)
        vigore_da = vm.group(1) if vm else ""
        bm = re.search(r"<mainBody>(.*?)</mainBody>", corpo, re.S)
        if not bm:
            continue
        testo = re.sub(r"<[^>]+>", "", bm.group(1))
        testo = _pulisci(testo)
        # il testo apre con "NOME CODICE \n Art. N. \n (Rubrica)." — separa la rubrica
        intest = (doc_prefix or nome_codice).upper()
        testo = re.sub(r"^" + re.escape(intest) + r"\s*", "", testo, flags=re.I).strip()
        testo = re.sub(r"^Art\.\s*[0-9a-z\.\- ]+\.?\s*\n?", "", testo).strip()
        rubrica, testo = _separa_rubrica(testo)
        rubrica, natura = pulisci_rubrica(rubrica)
        voce = {"token": token, "rubrica": rubrica, "vigore_da": vigore_da, "testo": testo}
        if natura:
            voce["natura"] = natura
        articoli.append(voce)
    if articoli:
        return articoli
    # Fallback per gli atti che NON usano gli attachment-doc (misurato: la Costituzione
    # arriva come <article eId="art_N"> dentro il body principale; le leggi speciali della
    # v0.23 idem, con gli articoli aggiunti come eId="art_10-bis" — il trattino va ammesso,
    # altrimenti L. 241/1990 esce con 31 articoli su 51). Qui NON c'e' una data per articolo
    # nell'AKN: la da' l'API Open Data in `costruisci` (vedi `data_articoli_api`); la stima dai
    # rimandi di nota resta solo come diagnostica.
    eventi = {m.group(2): m.group(1) for m in
              re.finditer(r"<eventRef date=\"(\d{4}-\d{2}-\d{2})\" eId=\"[^\"]+\" source=\"([^\"]+)\"", xml)}
    originale = eventi.get("ro1") or ""
    if not originale:
        om = re.search(r"<FRBRWork>.*?<FRBRdate date=\"(\d{4}-\d{2}-\d{2})\"", xml, re.S)
        originale = om.group(1) if om else ""
    segmento = xml
    if solo_allegati:
        taglio = xml.find("<attachments")
        segmento = xml[taglio:] if taglio > 0 else ""
    articoli = _togli_decreto_di_approvazione(_articoli_monoblocco(segmento, eventi, originale))
    for a in articoli:
        a["rubrica"], natura = pulisci_rubrica(a["rubrica"])
        if natura:
            a["natura"] = natura
    return articoli


_RX_NOTA = re.compile(r"\(\(?\s*(\d{1,3})\s*\)\)?")


def data_articolo_monoblocco(corpo_xml: str, eventi: dict, originale: str):
    """STIMA (vigore_da, base) di un articolo di un atto MONOBLOCCO (senza FRBRWork per articolo).

    ⚠️ Dalla v0.29 e' solo DIAGNOSTICA: il confronto del 23/09/2026 con l'API Open Data (fonte della
    data per articolo) ha trovato la stima anteriore alla data vera nella maggioranza dei casi — le
    note datano l'atto modificante (pubblicazione), non l'entrata in vigore, e ignorano le modifiche
    senza rimando; «originale» e' la data dell'atto, non della sua vigenza (CCII: 2019 contro 2022).
    `costruisci` la sostituisce con la data dell'API o la lascia vuota. Il testo che segue descrive
    come la si calcola.

    Misurato il 18/09/2026 su D.L. 1/2012, L. 108/1996, TUB, Cod. consumo: le evidenze di
    modifica «((...))» portano di regola un rimando di nota «((N))» / «(N)», e la nota N
    corrisponde all'evento `rpN` della <lifecycle> dell'atto, che ha la data. Tre casi:

      * nessuna evidenza di modifica (<ins>/<del>/«((») → l'articolo e' quello ORIGINARIO:
        vigore_da = data dell'atto (evento ro1)                                → base "originale";
      * ogni <ins> testuale e' seguito da un rimando di nota nello stesso <p> → vigore_da = la
        data piu' recente fra le note                                          → base "note";
      * un <ins> senza nota (visto: art. 2 co. 4 L. 108/1996, mod. D.L. 70/2011 senza «((13))»)
        → NON si sa quando e' cambiato: vigore_da vuoto, e `articolo()` ricade sulla regola
        prudente del consolidamento                                          → base "ignota".
    Tempus regit actum non si negozia: nel dubbio il locale tace e si va su normattiva datato.
    """
    ha_marcatori = ("<ins" in corpo_xml) or ("<del" in corpo_xml) or ("((" in corpo_xml)
    if not ha_marcatori:
        return (originale, "originale") if originale else ("", "ignota")
    note = set()
    tutti_datati = True
    for pm in re.finditer(r"<p\b[^>]*>(.*?)</p>", corpo_xml, re.S):
        p = pm.group(1)
        pezzi = re.split(r"(<ins\b[^>]*>.*?</ins>|<del\b[^>]*>.*?</del>)", p, flags=re.S)
        for i, pezzo in enumerate(pezzi):
            if not pezzo.startswith(("<ins", "<del")):
                continue
            dentro = re.sub(r"<[^>]+>", "", pezzo).strip()
            if _RX_NOTA.fullmatch(dentro.strip()):
                note.add(int(_RX_NOTA.fullmatch(dentro.strip()).group(1)))
                continue
            coda = "".join(pezzi[i + 1:])
            mn = _RX_NOTA.search(re.sub(r"<[^>]+>", "", coda))
            if mn:
                note.add(int(mn.group(1)))
            else:
                tutti_datati = False
        for mn in _RX_NOTA.finditer(re.sub(r"<[^>]+>", "", p)):
            note.add(int(mn.group(1)))
    date = [eventi.get(f"rp{n}") for n in note]
    date = [d for d in date if d]
    if tutti_datati and date:
        return max(date), "note"
    return "", "ignota"


# ============================================================ versioni dell'atto (Fase 4, D5.4)

_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
         "settembre", "ottobre", "novembre", "dicembre"]
_SIGLE_AKN = {
    "LEGGE": "L.", "DECRETO-LEGGE": "D.L.", "DECRETO_LEGGE": "D.L.", "DECRETO_LEGISLATIVO": "D.Lgs.",
    "DECRETO_DEL_PRESIDENTE_DELLA_REPUBBLICA": "D.P.R.", "REGIO_DECRETO": "R.D.",
    "REGIO_DECRETO_LEGGE": "R.D.L.", "LEGGE_COSTITUZIONALE": "L. cost.", "DECRETO": "D.M.",
    "DECRETO_MINISTERIALE": "D.M.", "DECRETO_DEL_PRESIDENTE_DEL_CONSIGLIO_DEI_MINISTRI": "D.P.C.M.",
    "CIRCOLARE": "Circ.", "PROVVEDIMENTO": "Provv.", "DELIBERA": "Delib.", "REGOLAMENTO": "Reg.",
}


def _data_italiana(iso: str) -> str:
    try:
        d = _dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.day} {_MESI[d.month - 1]} {d.year}"


def formatta_atto_akn(href: str) -> str:
    """'/akn/it/act/DECRETO-LEGGE/stato/2010-12-29/225/!main' → 'D.L. 29 dicembre 2010, n. 225'.

    Gli href della <references> dell'AKN normattiva sono l'unico posto in cui l'export dice CHI ha
    modificato l'atto (gli `showAs` sono vuoti). Un href vuoto resta vuoto: chi legge deve
    vedere che il modificante non e' indicato, non un nome inventato."""
    m = re.search(r"/akn/it/act/([^/]+)/([^/]+)/(\d{4}-\d{2}-\d{2})/([^/]+)/", str(href or ""))
    if not m:
        return ""
    tipo, ente, data, num = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
    sigla = _SIGLE_AKN.get(tipo)
    if not sigla:
        sigla = tipo.replace("_", " ").replace("-", " ").title()
    ente_txt = ""
    if ente and ente.lower() != "stato" and tipo in ("DECRETO", "DECRETO_MINISTERIALE", "CIRCOLARE", "PROVVEDIMENTO", "DELIBERA", "REGOLAMENTO"):
        ente_txt = " " + unescape(ente).replace("_", " ").replace("'", "'").title()
    return f"{sigla}{ente_txt} {_data_italiana(data)}, n. {num}"


def estrai_versioni(xml: str) -> list:
    """Le date di versione dell'atto dai metadati AKN (piano D5.4).

    <lifecycle> porta un <eventRef date=… source="ro1|rpN"> per ogni evento (originale + ogni
    modifica); <references> associa a ro1/rpN l'href dell'atto modificante. Piu' eventi nello
    stesso giorno = UNA versione (il testo alla data e' uno solo). Misurato il 18/09/2026 sul
    D.Lgs. 28/2010: 14 eventi → 14 date, 13 passiveRef con href (uno vuoto).
    Si legge solo la parte PRIMA degli <attachments> (nei codici doc-per-articolo i singoli
    <doc> non hanno lifecycle, ma per sicurezza non si mescolano)."""
    testa = xml
    taglio = xml.find("<attachments")
    if taglio > 0:
        testa = xml[:taglio]
    refs = {}
    for m in re.finditer(r"<(?:original|passiveRef)\s+eId=\"([^\"]+)\"\s+href=\"([^\"]*)\"", testa):
        refs[m.group(1)] = m.group(2)
    per_data = {}
    for m in re.finditer(r"<eventRef\s+date=\"(\d{4}-\d{2}-\d{2})\"[^>]*source=\"([^\"]+)\"", testa):
        data, src = m.group(1), m.group(2)
        voce = per_data.setdefault(data, {"data": data, "eventi": [], "atti": []})
        voce["eventi"].append(src)
        if src.startswith("rp"):
            atto = formatta_atto_akn(refs.get(src, ""))
            voce["atti"].append(atto or f"atto modificante non indicato ({src})")
    out = []
    for data in sorted(per_data):
        v = per_data[data]
        evento = v["eventi"][0] if len(v["eventi"]) == 1 else "+".join(v["eventi"])
        atti = []
        for a in v["atti"]:
            if a not in atti:
                atti.append(a)
        out.append({"data": data, "evento": evento, "atto_modificante": "; ".join(atti) or None})
    return out


def versione_alla_data(slug: str, data_iso: str, dati: dict = None):
    """La versione dell'atto in vigore a `data_iso`: {"inizio", "fine" (None = vigente), "atto_modificante"}.

    Confini a livello di ATTO (non di articolo): ogni modifica di qualunque articolo apre una
    versione nuova. E' prudente per costruzione — un articolo non toccato da una versione ha lo
    stesso testo prima e dopo — e rende la cache dello storico sicura: dentro una versione il
    testo di un articolo e' UNO. None se l'indice non porta versioni o la data precede l'atto."""
    if dati is None:
        _, dati = _indice(slug)
    if not dati:
        return None
    versioni = (dati.get("_meta") or {}).get("versioni") or []
    if not versioni or not data_iso:
        return None
    date = [v["data"] for v in versioni]
    if data_iso < date[0]:
        return None
    k = max(i for i, d in enumerate(date) if d <= data_iso)
    return {"inizio": date[k], "fine": date[k + 1] if k + 1 < len(date) else None,
            "atto_modificante": versioni[k].get("atto_modificante"), "indice": k, "totale": len(date)}


def leggi_snapshot(base: Path, slug: str):
    """(meta, {token: {"testo", "rubrica", "vigore_da"}}) di uno snapshot su disco (per corpus_diff)."""
    p = Path(base) / f"{slug}-indice.json"
    if not p.exists():
        return None, {}
    try:
        dati = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None, {}
    meta = dati.get("_meta") or {}
    out = {}
    testi_file = {}
    for token, voce in (dati.get("articoli") or {}).items():
        f = Path(base) / voce.get("file", "")
        if f not in testi_file:
            testi_file[f] = f.read_text(encoding="utf-8") if f.exists() else ""
        m = re.search(rf"^### Art\. {re.escape(token)}(?: — [^\n]*)?\n(?:<!--[^\n]*-->\n)?\n?(.*?)(?=^### Art\. |\Z)",
                      testi_file[f], re.M | re.S)
        out[token] = {"testo": (m.group(1).strip() if m else ""), "rubrica": voce.get("rubrica", ""),
                      "vigore_da": voce.get("vigore_da", ""), "datazione": voce.get("datazione")}
    return meta, out


# ============================================================ costruzione

def _base_num(token: str) -> int:
    m = re.match(r"(\d+)", token)
    return int(m.group(1)) if m else 0


def _ord_token(token: str):
    """Ordinamento: numero base, poi il suffisso nell'ordine latino."""
    LATINI = ["", "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies",
              "novies", "decies", "undecies", "duodecies", "terdecies", "quaterdecies",
              "quinquiesdecies", "sexiesdecies", "septiesdecies", "octiesdecies"]
    m = re.match(r"(\d+)(?:\.(\d+))?(?:-(.+))?$", token)
    if not m:
        return (99999, 99, 0, 0)
    suff = m.group(3) or ""
    sub = 0
    sm = re.match(r"([a-z]+)\.(\d+)$", suff)
    if sm:
        suff, sub = sm.group(1), int(sm.group(2))
    pos = LATINI.index(suff) if suff in LATINI else 90
    # «69.1» (TUB) viene dopo il 69 e i suoi -bis…: (69, 99, 1)
    if m.group(2):
        return (int(m.group(1)), 99, int(m.group(2)), 0)
    return (int(m.group(1)), pos, 0, sub)


def scarica_akn(slug: str) -> str:
    """Scarica l'AKN da normattiva: risolve l'URN, poi chiama caricaAKN.

    I parametri di caricaAKN (dataGU, codiceRedaz) NON sono cablati: si leggono dalla
    pagina risolta, che li espone nel proprio link di export. Cablarli a mano e' il modo
    piu' silenzioso di scaricare l'atto sbagliato.
    """
    import urllib.request
    from http.cookiejar import CookieJar
    cfg = CODICI[slug]
    jar = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (StudioCoccolo codice_locale)")]
    html = op.open(f"https://www.normattiva.it/uri-res/N2Ls?{cfg['urn']}!vig=",
                   timeout=60).read().decode("utf-8", errors="replace")
    m = re.search(r"caricaAKN\?dataGU=(\d{8})&(?:amp;)?codiceRedaz=([A-Z0-9]+)", html)
    if not m:
        raise SystemExit(f"[codice_locale] la pagina risolta per {slug} non espone il link "
                         "caricaAKN: struttura cambiata, aggiornare lo script.")
    oggi = _oggi().strftime("%Y%m%d")
    url = (f"https://www.normattiva.it/do/atto/caricaAKN?dataGU={m.group(1)}"
           f"&codiceRedaz={m.group(2)}&dataVigenza={oggi}")
    return op.open(url, timeout=300).read().decode("utf-8", errors="replace")


# ============================================================ datazione per articolo (API Open Data)

#: API Open Data di normattiva (nessuna autenticazione, verificata il 13 e il 23/09/2026):
#: `POST atto/dettaglio-atto-urn {"urn": "<urn>~art<N>"}` → `articoloDataInizioVigenza` (AAAAMMGG) della
#: versione vigente dell'articolo, `articoloDataFineVigenza` (99999999 = vigente) e `articoloHtml`.
API_OPENDATA = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1"
API_TIMEOUT = 10.0
#: Richieste in parallelo verso l'API (build una tantum al rilascio; il refresh settimanale chiede
#: solo gli articoli il cui testo e' cambiato). 6 = ~0,1 s ad articolo, misurato il 23/09/2026.
API_WORKERS = 6
#: Interruttore: se i primi N articoli di un atto falliscono tutti, l'API e' giu' (o non conosce
#: l'atto, come il TUEL): inutile aspettare il timeout su altri 400 articoli.
API_STOP_DOPO = 25


def hash_testo(testo: str) -> str:
    """Impronta breve del testo di un articolo (spazi normalizzati): se il testo non cambia fra due
    snapshot, la versione e' la stessa e la data dell'API si riporta senza richiederla."""
    return hashlib.sha1(" ".join(str(testo or "").split()).encode("utf-8")).hexdigest()[:12]


#: Attese fra i tentativi sui guasti transitori dell'API (5xx, 429, timeout). Misurato il 23/09/2026:
#: sotto carico sostenuto l'API alterna finestre di errori a finestre sane; un 404 non si ritenta.
API_ATTESE = (2, 6, 15)


def _api_esito(urn_art: str, timeout: float = API_TIMEOUT, attese=API_ATTESE):
    """(atto | None, esito) — esito: 'ok', 'assente' (404/400/risposta vuota), 'errore:<causa>'."""
    import urllib.error
    import urllib.request
    corpo = json.dumps({"urn": urn_art}).encode("utf-8")
    ultimo = "errore"
    for i in range(len(attese) + 1):
        req = urllib.request.Request(
            API_OPENDATA + "/atto/dettaglio-atto-urn", data=corpo,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (StudioCoccolo codice_locale)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", errors="replace"))
            atto = ((d or {}).get("data") or {}).get("atto") if isinstance(d, dict) else None
            if isinstance(atto, dict) and (atto.get("articoloHtml") or atto.get("articoloDataInizioVigenza")):
                return atto, "ok"
            return None, "assente"
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None, "assente"
            ultimo = f"errore:{e.code}"
        except Exception as e:  # timeout, reset, DNS: transitorio
            ultimo = f"errore:{e.__class__.__name__}"
        if i < len(attese):
            time.sleep(attese[i])
    return None, ultimo


def api_articolo(urn_art: str, timeout: float = API_TIMEOUT):
    """`data.atto` del dettaglio-atto-urn per un articolo, o None (assente, rete, JSON inatteso)."""
    return _api_esito(urn_art, timeout)[0]


def _data8(s) -> str:
    """'20160415' → '2016-04-15'; '' se assente, 99999999 o fuori scala."""
    s = str(s or "").strip()
    if not re.fullmatch(r"\d{8}", s) or s.startswith("9999"):
        return ""
    try:
        d = _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return ""
    return d.isoformat() if d.year >= 1800 else ""


def _parole(testo: str) -> set:
    t = unicodedata.normalize("NFKD", unescape(re.sub(r"<[^>]+>", " ", str(testo or ""))).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 3}


_RX_FORMULA_FINALE = re.compile(r"(?is)\b(?:il presente decreto|la presente legge)[^.]{0,80}?munit[oa]\s+del\s+sigillo.*$")


def stesso_articolo(testo_locale: str, testo_api: str) -> bool:
    """Il testo dell'API e' lo stesso articolo (stessa versione) del testo locale? L'API puo' sbagliare
    articolo (misurato: «~art69.1» del TUB restituisce l'art. 69) e il suo HTML porta numero e rubrica
    in testa: si confrontano gli insiemi di parole, esigendo che quasi tutto il locale sia nell'API.
    L'ultimo articolo, nell'API, si porta dietro la formula di chiusura dell'atto («Il presente decreto,
    munito del sigillo dello Stato…»): non e' testo dell'articolo e si toglie prima del confronto."""
    testo_api = _RX_FORMULA_FINALE.sub("", unescape(re.sub(r"<[^>]+>", " ", str(testo_api or ""))))
    a, b = _parole(testo_locale), _parole(testo_api)
    if not a or not b:
        return False
    comuni = len(a & b)
    return comuni / len(a) >= 0.9 and comuni / len(b) >= 0.6


def data_articoli_api(urn: str, articoli: list, *, precedente: dict = None, usa_api: bool = True,
                      fetch=None, workers: int = API_WORKERS, oggi: str = None) -> dict:
    """Data della versione vigente per ogni articolo MONOBLOCCO (quelli con la chiave `datazione`).

    Regole (tempus regit actum non si negozia):
      * data SOLO dall'API, e solo se il testo dell'API e' lo stesso articolo del locale;
      * testo identico allo snapshot precedente datato dall'API → la data si riporta (nessuna chiamata);
      * nessuna data affidabile → `vigore_da` vuoto, `datazione: ignota` (regola del consolidamento);
      * la stima dai rimandi di nota non entra mai nell'indice: serve solo a contare quante volte
        avrebbe sbagliato in anticipo (`stima_anteriore`, il caso pericoloso).
    Muta `articoli`; ritorna le statistiche. Mai eccezioni: l'API e' un di piu', non un requisito."""
    from concurrent.futures import ThreadPoolExecutor
    if fetch is None:
        chiama = _api_esito
    else:
        def chiama(u):
            atto = fetch(u)
            return atto, ("ok" if atto else "assente")
    oggi = oggi or _oggi().isoformat()
    prec = ((precedente or {}).get("articoli") or {}) if isinstance(precedente, dict) else {}
    st = {"monoblocco": 0, "api": 0, "riportati": 0, "ignota": 0, "falliti": 0, "testo_diverso": 0,
          "stima_anteriore": 0, "stima_posteriore": 0, "stima_uguale": 0, "interrotta": False, "chiamate": 0}
    stime, coda = {}, []
    for a in articoli:
        if "datazione" not in a:
            continue
        st["monoblocco"] += 1
        a["h"] = hash_testo(a.get("testo"))
        stime[id(a)] = a.get("vigore_da") or ""
        a["vigore_da"], a["datazione"] = "", "ignota"
        a.pop("vigore_a", None)
        p = prec.get(a["token"]) or {}
        if p.get("datazione") == "api" and p.get("h") == a["h"] and p.get("vigore_da"):
            a["vigore_da"], a["datazione"] = p["vigore_da"], "api"
            if p.get("vigore_a"):
                a["vigore_a"] = p["vigore_a"]
            st["riportati"] += 1
            continue
        coda.append(a)

    def _chiedi(a):
        # «!vig=<data dello snapshot>»: la versione vigente QUEL giorno, con la sua fine di vigenza. Senza
        # data l'API restituisce l'ULTIMA versione, anche futura (D.Lgs. 139/2024: 2027-01-01).
        tok = a["token"].replace("-", "")
        atto, esito = chiama(f"{urn}~art{tok}!vig={oggi}")
        return a, atto, esito

    if usa_api and coda:
        # Forma dell'URN che l'API riconosce: di norma quella datata del corpus; per alcuni atti (TUEL,
        # misurato il 23/09/2026) solo quella con il solo anno. Si prova sui primi articoli e si sceglie.
        # Per i decreti ministeriali l'API vuole l'autorita' «stato» (urn:nir:stato:decreto:2014-03-10;55),
        # non il ministero dell'URN normattiva.
        forme = []
        su_stato = re.sub(r"^urn:nir:[^:]+:", "urn:nir:stato:", urn)
        for f in (urn, re.sub(r":(\d{4})-\d{2}-\d{2};", r":\1;", urn), su_stato,
                  re.sub(r":(\d{4})-\d{2}-\d{2};", r":\1;", su_stato)):
            if f not in forme:
                forme.append(f)
        if len(forme) > 1:
            for campione in coda[:3]:
                tok = campione["token"].replace("-", "")
                scelta = None
                for f in forme:
                    esito_f = chiama(f"{f}~art{tok}!vig={oggi}")[1]
                    if esito_f == "ok":
                        scelta = f
                        break
                    if esito_f != "assente":      # errore di rete: non si decide su questo campione
                        break
                if scelta:
                    if scelta != urn:
                        urn = scelta
                        st["urn_api"] = scelta
                    break
        blocco = max(API_STOP_DOPO, workers * 4)
        riusciti = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for i in range(0, len(coda), blocco):
                for a, atto, esito in ex.map(_chiedi, coda[i:i + blocco]):
                    st["chiamate"] += 1
                    if esito != "ok":
                        chiave = "assenti" if esito == "assente" else "errori_rete"
                        st[chiave] = st.get(chiave, 0) + 1
                    inizio = _data8((atto or {}).get("articoloDataInizioVigenza"))
                    if not atto or not inizio or inizio > oggi:
                        st["falliti"] += 1
                        continue
                    if not stesso_articolo(a.get("testo"), atto.get("articoloHtml")):
                        st["testo_diverso"] += 1
                        continue
                    riusciti += 1
                    a["vigore_da"], a["datazione"] = inizio, "api"
                    fine = _data8(atto.get("articoloDataFineVigenza"))
                    if fine:
                        a["vigore_a"] = fine
                    stima = stime.get(id(a)) or ""
                    if stima:
                        k = "stima_anteriore" if stima < inizio else ("stima_posteriore" if stima > inizio else "stima_uguale")
                        st[k] += 1
                if riusciti == 0 and st["chiamate"] >= API_STOP_DOPO:
                    st["interrotta"] = True
                    break
    st["api"] = sum(1 for a in articoli if a.get("datazione") == "api") - st["riportati"]
    st["ignota"] = sum(1 for a in articoli if a.get("datazione") == "ignota")
    return st


def costruisci(slug: str, xml: str, dest: Path, *, datazione_api: bool = False, fetch=None) -> dict:
    """Scrive .md + indice di un atto dall'AKN. `datazione_api`: data per articolo dall'API Open Data
    (build di rilascio e refresh); senza, gli articoli monoblocco restano senza data salvo quelli il cui
    testo e' identico allo snapshot precedente gia' datato (mai la stima euristica)."""
    cfg = CODICI[slug]
    articoli = parse_akn(xml, cfg["nome"], cfg.get("doc_prefix", ""), bool(cfg.get("solo_corpo")),
                         bool(cfg.get("solo_allegati")))
    attesi = cfg.get("min_articoli", 50)
    if len(articoli) < attesi:
        raise SystemExit(f"[codice_locale] parse sospetto: {len(articoli)} articoli "
                         f"(attesi >= {attesi}) da un XML di {len(xml)} byte — non scrivo niente.")
    conta = {}
    for a in articoli:
        conta[a["token"]] = conta.get(a["token"], 0) + 1
    doppi = sorted((t for t, n in conta.items() if n > 1), key=_ord_token)
    if doppi:
        # Un numero doppio vuol dire due serie nello stesso AKN (corpo e allegati, decreto e testo
        # unico): l'indice ne terrebbe una a caso e il lookup l'altra. D.Lgs. 196/2003 senza
        # `solo_corpo`: gli allegati sovrascrivevano il codice. Si rifiuta, non si indovina.
        raise SystemExit(f"[codice_locale] {slug}: numeri di articolo DOPPI {doppi[:8]}"
                         f"{' …' if len(doppi) > 8 else ''} — serie concorrenti nello stesso AKN: configurare "
                         "`solo_corpo` o `solo_allegati` in codici.json. Non scrivo niente.")
    articoli.sort(key=lambda a: _ord_token(a["token"]))
    try:
        _, precedente = _indice(slug)
    except Exception:
        precedente = None
    stat_datazione = data_articoli_api(cfg["urn"], articoli, precedente=precedente,
                                       usa_api=datazione_api, fetch=fetch)
    dest.mkdir(parents=True, exist_ok=True)
    oggi = _oggi().isoformat()
    cm = re.search(r"CONSOLIDATED/(\d{4})(\d{2})(\d{2})", xml)
    consolidato = f"{cm.group(1)}-{cm.group(2)}-{cm.group(3)}" if cm else oggi

    indice = {}
    per_libro: dict[str, list[dict]] = {}
    for a in articoli:
        n = _base_num(a["token"])
        titolo_libro = cfg["libri"][-1][0]
        for titolo, lo, hi in cfg["libri"]:
            if lo <= n <= hi:
                titolo_libro = titolo
                break
        per_libro.setdefault(titolo_libro, []).append(a)

    file_scritti = []
    for i, (titolo, lo, hi) in enumerate(cfg["libri"], start=1):
        gruppo = per_libro.get(titolo)
        if not gruppo:
            continue
        nome_file = f"{slug}-{i}.md" if len(cfg["libri"]) > 1 else f"{slug}.md"
        righe = [
            f"# {cfg['nome']} — {titolo}",
            "",
            f"<!-- codice: {slug} · snapshot: {oggi} · consolidato-normattiva: {consolidato}"
            f" · fonte: normattiva caricaAKN ({cfg['urn']}) -->",
            "",
            "> **File di DATI, generato da macchina** (`scripts/codice_locale.py`): testo",
            "> ufficiale normattiva, letterale, con la data di vigenza di ciascun articolo.",
            "> Non si modifica a mano: si rigenera. Le doppie parentesi ((...)) sono le",
            "> evidenze di modifica di normattiva, mantenute apposta.",
            "",
        ]
        for a in gruppo:
            # la rubrica sta su UNA riga: una riga in piu' spezzava il formato «### Art. N — …»
            rubrica = " ".join(str(a["rubrica"] or "").split())
            rub = f" — {rubrica}" if rubrica else ""
            righe.append(f"### Art. {a['token']}{rub}")
            if a["vigore_da"]:
                righe.append(f"<!-- vigore_da: {a['vigore_da']} -->")
            righe.append("")
            righe.append(a["testo"])
            righe.append("")
            voce = {"file": nome_file, "rubrica": rubrica, "vigore_da": a["vigore_da"]}
            for k in ("datazione", "vigore_a", "natura", "h"):
                if a.get(k):
                    voce[k] = a[k]
            indice[a["token"]] = voce
        (dest / nome_file).write_text("\n".join(righe) + "\n", encoding="utf-8")
        file_scritti.append(nome_file)

    versioni = estrai_versioni(xml)
    meta = {"codice": slug, "nome": cfg["nome"], "snapshot": oggi, "formato": FORMATO_INDICE,
            "consolidato_normattiva": consolidato, "articoli": len(articoli),
            "file": file_scritti, "vita_utile_giorni": VITA_UTILE_GIORNI,
            "vita_refresh_giorni": VITA_REFRESH_GIORNI,
            "urn": cfg["urn"], "versioni": versioni,
            "ultima_versione": versioni[-1]["data"] if versioni else None}
    if stat_datazione.get("monoblocco"):
        meta["datazione"] = {k: v for k, v in stat_datazione.items() if v or k in ("api", "ignota")}
        meta["datazione"]["fonte"] = "API Open Data normattiva (articoloDataInizioVigenza), validata sul testo"
    (dest / f"{slug}-indice.json").write_text(
        json.dumps({"_meta": meta, "articoli": indice}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    return meta


# ============================================================ lookup

def _indice(slug: str):
    """(dir, indice) del piu' fresco fra runtime e seed, o (None, None). Un indice del formato corrente
    batte sempre uno di formato piu' vecchio (vedi FORMATO_INDICE), a prescindere dalla data."""
    candidati = []
    for base in (_dir_runtime(), _dir_seed()):
        p = base / f"{slug}-indice.json"
        if not p.exists():
            continue
        try:
            dati = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        meta = dati.get("_meta", {}) if isinstance(dati, dict) else {}
        candidati.append((int(meta.get("formato") or 1) >= FORMATO_INDICE, str(meta.get("snapshot", "")), base, dati))
    if not candidati:
        return None, None
    attuale = [c for c in candidati if c[0]] or candidati
    migliore = None
    for c in attuale:
        if migliore is None or c[1] > migliore[1]:
            migliore = c
    return migliore[2], migliore[3]


def eta_snapshot(slug: str):
    _, dati = _indice(slug)
    if not dati:
        return None
    try:
        snap = _dt.date.fromisoformat(dati["_meta"]["snapshot"])
    except (KeyError, ValueError):
        return None
    return (_oggi() - snap).days


_MANIFEST = {}


def manifest_corpus() -> dict:
    """v0.30: il manifest del corpus pubblico in vigore (sincronizzato, altrimenti quello del bundle)."""
    for p in (stato_root() / "normativa" / "corpus-manifest.json", WIKI / "normativa" / "corpus-manifest.json"):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if _MANIFEST.get("chiave") == (str(p), mt):
            return _MANIFEST["dati"]
        try:
            dati = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(dati, dict) and dati.get("schema") == 1:
            _MANIFEST.update(chiave=(str(p), mt), dati=dati)
            return dati
    return {}


def voce_manifest(slug: str, dati: dict = None) -> dict:
    """La voce del manifest per `slug`, SOLO se descrive lo stesso snapshot che il lookup sta usando."""
    if dati is None:
        _, dati = _indice(slug)
    voce = (manifest_corpus().get("atti") or {}).get(slug) or {}
    snap = str(((dati or {}).get("_meta") or {}).get("snapshot") or "")
    return voce if voce and str(voce.get("snapshot") or "") == snap else {}


def in_movimento(slug: str, token: str = "") -> bool:
    """L'articolo (o l'atto) e' toccato da una legge pubblicata in G.U. e non ancora consolidata?"""
    voce = (manifest_corpus().get("atti") or {}).get(slug) or {}
    if not voce.get("in_movimento"):
        return False
    arts = [str(a) for a in voce.get("articoli_in_movimento") or []]
    return not arts or "*" in arts or not token or _norm_token("art. " + str(token)) in arts


def eta_verifica(slug: str):
    """Giorni dall'ultima volta che lo snapshot e' stato VERIFICATO come corrente: il download
    (`snapshot`) oppure la conferma senza download dell'API «atti aggiornati» (`confermato_il`,
    scritta da corpus_refresh.py). E' l'eta' che decide il refresh e la cascata «vale oggi?»."""
    _, dati = _indice(slug)
    if not dati:
        return None
    meta = dati.get("_meta") or {}
    date = []
    for v in (meta.get("snapshot"), meta.get("confermato_il"), voce_manifest(slug, dati).get("verificato_il")):
        try:
            date.append(_dt.date.fromisoformat(str(v or "")))
        except ValueError:
            pass
    if not date:
        return None
    return (_oggi() - max(date)).days


def ultima_verifica(slug: str) -> str:
    """AAAA-MM-GG dell'ultima verifica dello snapshot (download o conferma API), '' se ignota."""
    _, dati = _indice(slug)
    meta = (dati or {}).get("_meta") or {}
    date = [str(meta.get(k) or "") for k in ("snapshot", "confermato_il")]
    date.append(str(voce_manifest(slug, dati).get("verificato_il") or ""))
    date = [d for d in date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]
    return max(date) if date else ""


def articolo(token: str, slug: str = "cpc", data_evento: str = "") -> dict:
    """Il testo locale di un articolo, o un verdetto NON_DISPONIBILE con il motivo.

    Regole di ammissibilita' (tutte dichiarate nell'esito):
      * con data-evento: solo se data-evento >= vigore_da dell'articolo — il testo alla
        data coincide allora col vigente. Un caso pre-riforma va su normattiva datato;
      * se l'API ha dichiarato una fine di vigenza (`vigore_a`, modifica gia' pubblicata con
        decorrenza futura), una data-evento successiva non si serve dal locale;
      * snapshot oltre la vita massima (45 gg dall'ultima verifica): v0.29 — si serve lo stesso,
        con `snapshot_oltre_vita: true` e la data dell'ultima conferma. Prima il locale taceva e
        su Cowork, con la rete bloccata, l'avvocato restava senza testo; ora il Livello 0 tratta
        come residue le claim fondanti su quel testo e chi legge vede «SNAPSHOT NON CONFERMATO».
    """
    token = _norm_token("art. " + str(token))
    base, dati = _indice(slug)
    if not dati:
        return {"verdetto": "NON_DISPONIBILE", "motivo": f"nessuno snapshot locale per {slug}"}
    eta = eta_verifica(slug)
    if eta is None:
        return {"verdetto": "NON_DISPONIBILE",
                "motivo": f"snapshot {slug} senza data leggibile: indice da rigenerare, vai in rete"}
    oltre_vita = eta > VITA_UTILE_GIORNI
    voce = dati["articoli"].get(token)
    if not voce:
        return {"verdetto": "NON_DISPONIBILE",
                "motivo": f"art. {token} non nello snapshot {slug} (verifica live: potrebbe non esistere)"}
    if data_evento and voce.get("vigore_da") and data_evento < voce["vigore_da"]:
        return {"verdetto": "NON_DISPONIBILE",
                "motivo": (f"data-evento {data_evento} anteriore alla versione vigente "
                           f"dell'articolo ({voce['vigore_da']}): serve il testo storico, "
                           "vai su normattiva con URN datato")}
    if data_evento and voce.get("vigore_a") and data_evento > voce["vigore_a"]:
        return {"verdetto": "NON_DISPONIBILE",
                "motivo": (f"data-evento {data_evento} successiva alla fine di vigenza di questo testo "
                           f"({voce['vigore_a']}, modifica con decorrenza differita): vai su normattiva con URN datato")}
    if data_evento and not voce.get("vigore_da"):
        # atto senza data per articolo (leggi speciali, Costituzione): il testo alla data
        # coincide col vigente SOLO se la data-evento e' posteriore all'ultimo consolidamento
        # dell'atto — prima di quella data non sappiamo se l'articolo e' cambiato.
        consolidato = str(dati["_meta"].get("consolidato_normattiva") or "")
        if consolidato and data_evento < consolidato:
            return {"verdetto": "NON_DISPONIBILE",
                    "motivo": (f"data-evento {data_evento} anteriore al consolidamento dell'atto "
                               f"({consolidato}) e lo snapshot non porta la data della versione "
                               "dell'articolo: vai su normattiva con URN datato")}
    f = base / voce["file"]
    if not f.exists():
        return {"verdetto": "NON_DISPONIBILE", "motivo": f"file {voce['file']} mancante"}
    testo_file = f.read_text(encoding="utf-8")
    # " — [^\n]*" e non " — .*": con re.S il punto mangerebbe anche i newline e il
    # gruppo del testo uscirebbe vuoto (visto succedere: la validazione confrontava "").
    m = re.search(rf"^### Art\. {re.escape(token)}(?: — [^\n]*)?\n(?:<!--[^\n]*-->\n)?\n?(.*?)(?=^### Art\. |\Z)",
                  testo_file, re.M | re.S)
    if not m:
        return {"verdetto": "NON_DISPONIBILE", "motivo": f"art. {token} non trovato in {voce['file']}"}
    meta = dati["_meta"]
    esito = {
        "verdetto": "OK", "token": token, "codice": slug,
        "rubrica": voce.get("rubrica", ""), "vigore_da": voce.get("vigore_da", ""),
        "datazione": voce.get("datazione") or ("articolo" if voce.get("vigore_da") else "consolidamento"),
        "abrogato": bool(re.match(r"^\(?\(?\s*ARTICOLO\s+ABROGATO", m.group(1).strip(), re.I)),
        "testo": m.group(1).strip(),
        "snapshot": meta["snapshot"], "consolidato": meta["consolidato_normattiva"],
        "fonte": (f"testo locale datato — normattiva caricaAKN, consolidato al "
                  f"{meta['consolidato_normattiva']}, snapshot del {meta['snapshot']}"),
        "file": str(f), "snapshot_oltre_vita": oltre_vita,
    }
    if voce.get("vigore_a"):
        esito["vigore_a"] = voce["vigore_a"]
    if voce.get("natura"):
        esito["natura"] = voce["natura"]
    esito["verificato_il"] = ultima_verifica(slug)
    if in_movimento(slug, token):
        esito["in_movimento"] = True
        esito["avviso"] = ("ATTO IN MOVIMENTO: una legge pubblicata in Gazzetta Ufficiale lo modifica e normattiva "
                           "non l'ha ancora recepita nel testo consolidato — leggi la legge modificante")
    if oltre_vita:
        conf = ultima_verifica(slug)
        esito["snapshot_confermato_il"] = conf
        esito["avviso"] = (f"SNAPSHOT NON CONFERMATO dal {conf} ({eta} gg, oltre i {VITA_UTILE_GIORNI} di vita): "
                           "il testo puo' essere cambiato — conferma su normattiva (claim fondanti: residue)")
        esito["fonte"] += f" — SNAPSHOT NON CONFERMATO dal {conf}"
    return esito


def stato() -> list[dict]:
    out = []
    for slug in CODICI:
        base, dati = _indice(slug)
        if not dati:
            out.append({"codice": slug, "presente": False})
            continue
        m = dati["_meta"]
        eta = eta_snapshot(slug)
        ev = eta_verifica(slug)
        out.append({"codice": slug, "presente": True, "articoli": m["articoli"],
                    "snapshot": m["snapshot"], "eta_giorni": eta,
                    "confermato_il": m.get("confermato_il"), "eta_verifica": ev,
                    "fresco": ev is not None and ev <= VITA_UTILE_GIORNI,
                    "snapshot_oltre_vita": ev is None or ev > VITA_UTILE_GIORNI,
                    "datazione": ({k: m["datazione"].get(k) for k in ("monoblocco", "api", "riportati", "ignota")}
                                  if isinstance(m.get("datazione"), dict) else None),
                    "refresh_dovuto": ev is None or ev > VITA_REFRESH_GIORNI,
                    "versioni": len(m.get("versioni") or []),
                    "dove": str(base)})
    return out


def data_indice_esistente(slug: str, base: Path = None, fetch=None) -> dict:
    """Ridata via API gli articoli monoblocco di un indice GIA' costruito (senza riscaricare l'AKN):
    per completare una datazione interrotta (API giu' al momento del refresh). Scrive solo l'indice
    (fonte del gate); il commento <!-- vigore_da --> del .md resta quello della costruzione."""
    if base is None:
        base, _ = _indice(slug)
    if base is None:
        return {"errore": f"nessuno snapshot per {slug}"}
    p = Path(base) / f"{slug}-indice.json"
    dati = json.loads(p.read_text(encoding="utf-8"))
    _, testi = leggi_snapshot(Path(base), slug)
    articoli = []
    for tok, voce in (dati.get("articoli") or {}).items():
        if "datazione" not in voce:
            continue
        articoli.append({"token": tok, "testo": (testi.get(tok) or {}).get("testo", ""),
                         "vigore_da": voce.get("vigore_da", ""), "datazione": voce.get("datazione")})
    st = data_articoli_api(dati["_meta"].get("urn") or CODICI[slug]["urn"], articoli, precedente=dati, fetch=fetch)
    for a in articoli:
        voce = dati["articoli"][a["token"]]
        voce["vigore_da"], voce["datazione"], voce["h"] = a["vigore_da"], a["datazione"], a["h"]
        voce.pop("vigore_a", None)
        if a.get("vigore_a"):
            voce["vigore_a"] = a["vigore_a"]
    if st.get("monoblocco"):
        dati["_meta"]["datazione"] = {k: v for k, v in st.items() if v or k in ("api", "ignota")}
        dati["_meta"]["datazione"]["fonte"] = "API Open Data normattiva (articoloDataInizioVigenza), validata sul testo"
    p.write_text(json.dumps(dati, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return st


# ============================================================ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="Codici locali datati (normattiva AKN)")
    ap.add_argument("--costruisci", metavar="SLUG", choices=sorted(CODICI))
    ap.add_argument("--da-xml", metavar="FILE", help="AKN gia' scaricato (altrimenti scarica)")
    ap.add_argument("--nel-repo", action="store_true",
                    help="scrive il seed in wiki-studio/normativa/testi/ (per il rilascio); "
                         "default: <stato_root>/testi/ (refresh runtime, fuori dal pacchetto)")
    ap.add_argument("--art", metavar="N", help='es. "163", "281-undecies"')
    ap.add_argument("--codice", default="cpc", choices=sorted(CODICI))
    ap.add_argument("--data-evento", default="")
    ap.add_argument("--senza-datazione", action="store_true",
                    help="non interrogare l'API Open Data per la data per articolo (gli articoli monoblocco "
                         "restano senza data, salvo i testi invariati gia' datati)")
    ap.add_argument("--data-api", metavar="SLUG", choices=sorted(CODICI),
                    help="ridata via API un indice gia' costruito (runtime, o seed con --nel-repo)")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.costruisci:
        xml = (Path(a.da_xml).read_text(encoding="utf-8") if a.da_xml
               else scarica_akn(a.costruisci))
        dest = _dir_seed() if a.nel_repo else _dir_runtime()
        meta = costruisci(a.costruisci, xml, dest, datazione_api=not a.senza_datazione)
        dz = meta.get("datazione") or {}
        print(f"COSTRUITO {meta['nome']}: {meta['articoli']} articoli · "
              f"consolidato normattiva {meta['consolidato_normattiva']} · "
              f"snapshot {meta['snapshot']}\n  → {dest} ({', '.join(meta['file'])})"
              + (f"\n  datazione per articolo: {dz.get('api', 0)} dall'API, {dz.get('riportati', 0)} riportati, "
                 f"{dz.get('ignota', 0)} senza data su {dz.get('monoblocco', 0)} monoblocco" if dz else ""))
        return 0

    if a.data_api:
        base = _dir_seed() if a.nel_repo else None
        st = data_indice_esistente(a.data_api, base)
        print(json.dumps(st, ensure_ascii=False) if a.json else
              (f"DATAZIONE {a.data_api}: {st.get('api', 0)} dall'API, {st.get('riportati', 0)} riportati, "
               f"{st.get('ignota', 0)} senza data su {st.get('monoblocco', 0)}" if "errore" not in st else st["errore"]))
        return 0 if "errore" not in st else 1

    if a.art:
        r = articolo(a.art, a.codice, a.data_evento)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["verdetto"] == "OK" else 1
        if r["verdetto"] != "OK":
            print(f"NON_DISPONIBILE {r['motivo']}")
            return 1
        print(f"OK art. {r['token']} {a.codice} — {r['rubrica'] or '(senza rubrica)'}")
        if r.get("snapshot_oltre_vita"):
            print(f"⚠ SNAPSHOT NON CONFERMATO dal {r.get('snapshot_confermato_il') or r.get('snapshot')}: "
                  "il testo puo' essere cambiato, conferma su normattiva")
        print(f"FONTE: {r['fonte']}")
        print(f"VIGORE DA: {r['vigore_da'] or 'n/d'}" + (f" (fino al {r['vigore_a']})" if r.get("vigore_a") else "")
              + (f" · datazione: {r.get('datazione')}" if r.get("datazione") else ""))
        print("TESTO:")
        print(r["testo"])
        return 0

    righe = stato()
    if a.json:
        print(json.dumps(righe, ensure_ascii=False, indent=1))
        return 0
    for r in righe:
        if not r["presente"]:
            print(f"{r['codice']:<14} — assente (python3 scripts/codice_locale.py --costruisci {r['codice']})")
        else:
            print(f"{r['codice']:<14} {r['articoli']} artt. · snapshot {r['snapshot']} "
                  f"({r['eta_giorni']} gg, {'fresco' if r['fresco'] else 'OLTRE VITA: servito con avviso'}"
                  f"{', refresh dovuto' if r.get('refresh_dovuto') else ''}) · {r.get('versioni', 0)} versioni · {r['dove']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
