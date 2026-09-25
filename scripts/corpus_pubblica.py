#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Costruttore del corpus PUBBLICO (piano v0.30, Fase 3): gira nella GitHub Action o, di riserva, sul Mac.

A COSA SERVE
------------
Il corpus normativo del plugin (testi ufficiali normattiva degli atti di `codici.json`) vive in un
repository GitHub pubblico, `piazzaed/studio-coccolo-corpus`, con le STESSE cartelle del plugin
(`wiki-studio/normativa/...`). Ogni settimana questo script lo tiene aggiornato:

  1. chiede a normattiva quali atti sono cambiati dall'ultima verifica (`ricerca/aggiornati`, una
     chiamata: l'API restituisce tutto in una pagina; se dichiara piu' atti di quanti ne consegna la
     settimana e' «incompleta» e `verificato_il` non avanza);
  2. riscarica SOLO gli atti cambiati (`codice_locale.scarica_akn` + `costruisci`, lo stesso codice
     del plugin; un download per URN: c.c. e preleggi condividono l'AKN), confronta articolo per
     articolo con lo snapshot precedente (`corpus_diff.confronta`) e scrive il changelog; se il
     confronto non trova differenze e il consolidato e' lo stesso, tiene lo snapshot vecchio e
     conferma soltanto (niente rumore nella storia git);
  3. legge i sommari della Gazzetta Ufficiale della finestra e, per gli atti normativi (codice
     redazionale con «G»), cerca le formule di modifica DIRETTE di un atto osservato («All'articolo N
     del codice … e' sostituito», «Al decreto legislativo …, n. …, sono apportate le seguenti
     modificazioni»): quegli atti diventano «in movimento» finche' il consolidato normattiva non
     recepisce la modifica. E' una rete di sicurezza, non la fonte: la fonte resta normattiva;
  4. verifica il risultato (`--verifica`: indici leggibili, formato, minimo di articoli, sha del
     manifest) e scrive `manifest.json` — sha256 e byte di ogni file, `snapshot`, `consolidato`,
     `verificato_il` (l'ultima data in cui normattiva ha confermato l'atto), `in_movimento`.

Il client (`corpus_sync.py`) scarica il manifest e poi solo i file il cui sha e' cambiato.

Uso:
  python3 scripts/corpus_pubblica.py --sonda                       # exit 3 se normattiva non risponde
  python3 scripts/corpus_pubblica.py --settimanale [--forza] [--solo cpc] [--report FILE]
  python3 scripts/corpus_pubblica.py --verifica
  python3 scripts/corpus_pubblica.py --init --repo DIR             # crea il repo pubblico dal plugin
  python3 scripts/corpus_pubblica.py --aggiorna-strumenti --repo DIR   # ricopia gli script nel repo
  python3 scripts/corpus_pubblica.py --pubblica --repo DIR         # riserva dal Mac: settimanale + git push
Solo stdlib, Python 3.9.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html import unescape
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codice_locale as cl  # noqa: E402
import corpus_diff as cd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
REPO_PUBBLICO = "piazzaed/studio-coccolo-corpus"
UA = "Mozilla/5.0 (StudioCoccolo corpus_pubblica)"
API_AGGIORNATI = cl.API_OPENDATA + "/ricerca/aggiornati"
GU = "https://www.gazzettaufficiale.it"
BACKOFF = (2, 8, 30)
#: finestra massima di ricerca delle modifiche (l'API accetta fino a 12 mesi; oltre 28 gg si ricostruisce)
FINESTRA_MAX_GIORNI = 28
#: ogni settimana si ricostruisce comunque 1/ROTAZIONE degli atti: normattiva cambia note e date di vigenza
#: senza segnalarlo nell'API «atti aggiornati» (visto il 25/09/2026 sul c.p.c.), cosi' nessun atto resta piu'
#: di ROTAZIONE settimane senza un confronto completo
ROTAZIONE = 4
#: dopo tanti giorni un «in movimento» mai recepito dal consolidato passa a «verifica_manuale»
MOVIMENTO_SCADENZA_GIORNI = 120
#: script copiati nel repo pubblico: tutto cio' che serve all'Action, niente di piu'
STRUMENTI = ("paths.py", "corpus.py", "codice_locale.py", "corpus_diff.py", "corpus_pubblica.py",
             "cassazione_indice.py", "diagnostica_rete.py")


# ---------------------------------------------------------------- percorsi

def dir_normativa(root: Path = ROOT) -> Path:
    return Path(root) / "wiki-studio" / "normativa"


def dir_testi(root: Path = ROOT) -> Path:
    return dir_normativa(root) / "testi"


def percorso_manifest(root: Path = ROOT) -> Path:
    return Path(root) / "manifest.json"


def percorso_movimento(root: Path = ROOT) -> Path:
    return dir_normativa(root) / "movimento.json"


def _oggi() -> _dt.date:
    return _dt.date.today()


def _ora_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for blocco in iter(lambda: f.read(1 << 20), b""):
            h.update(blocco)
    return h.hexdigest()


def _leggi_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _scrivi_json(p: Path, dati) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(dati, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(p))


def file_atto(slug: str, base: Path) -> list:
    """Nomi dei file di un atto (indice per ultimo), dall'indice stesso."""
    dati = _leggi_json(Path(base) / f"{slug}-indice.json", {}) or {}
    return list((dati.get("_meta") or {}).get("file") or []) + [f"{slug}-indice.json"]


# ---------------------------------------------------------------- normattiva: cosa e' cambiato

def _post_json(url: str, corpo: dict, timeout: float = 30) -> dict:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(corpo).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def atti_aggiornati(dal: _dt.date, al: _dt.date, post=None):
    """(chiavi (tipo, anno, numero) degli atti aggiornati, completo, n. dichiarati) — (None, False, 0) se l'API tace."""
    post = post or _post_json
    corpo = {"dataInizioAggiornamento": f"{dal.isoformat()}T00:00:00.000Z",
             "dataFineAggiornamento": f"{al.isoformat()}T23:59:59.000Z"}
    try:
        d = post(API_AGGIORNATI, corpo)
    except Exception:
        return None, False, 0
    lista = d.get("listaAtti") if isinstance(d, dict) else None
    if not isinstance(lista, list):
        return None, False, 0
    try:
        dichiarati = int(d.get("numeroAttiTrovati") or 0)
    except (TypeError, ValueError):
        dichiarati = 0
    chiavi = {cr_chiave_api(a) for a in lista if isinstance(a, dict)}
    return chiavi, dichiarati <= len(lista), dichiarati


def chiave_urn(urn: str):
    m = re.match(r"urn:nir:[^:]+:([^:]+):(\d{4})-\d{2}-\d{2};(\d+[a-z]*)", str(urn or ""))
    if not m:
        return None
    return (re.sub(r"[.\-_]+", " ", m.group(1)).strip().lower(), m.group(2), m.group(3).lower())


def cr_chiave_api(atto: dict):
    tipo = re.sub(r"[.\-_]+", " ", str(atto.get("denominazioneAtto") or "")).strip().lower()
    return (tipo, str(atto.get("annoProvvedimento") or ""), str(atto.get("numeroProvvedimento") or "").lower())


def sonda(post=None) -> bool:
    """normattiva risponde? (l'API «atti aggiornati» su un giorno qualunque)"""
    chiavi, _c, _n = atti_aggiornati(_oggi() - _dt.timedelta(days=2), _oggi() - _dt.timedelta(days=1), post=post)
    return chiavi is not None


# ---------------------------------------------------------------- Gazzetta Ufficiale: atti «in movimento»

_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
         "ottobre", "novembre", "dicembre"]
_TIPI_URN = {
    "legge": r"legge",
    "decreto.legislativo": r"decreto\s+legislativo",
    "decreto.legge": r"decreto[\s-]+legge",
    "decreto.del.presidente.della.repubblica": r"decreto\s+del\s+presidente\s+della\s+repubblica",
    "regio.decreto": r"regio\s+decreto",
    "decreto": r"decreto(?:\s+del\s+ministro\s+[\w\s']{3,40}?)?",
}
#: come i testi di legge chiamano per nome gli atti osservati (prima i nomi piu' lunghi che contengono i corti)
_NOMI = {
    "disp-att-cpc": [r"disposizioni\s+per\s+l['’]attuazione\s+del\s+codice\s+di\s+procedura\s+civile"],
    "disp-att-cc": [r"disposizioni\s+per\s+l['’]attuazione\s+del\s+codice\s+civile"],
    "preleggi": [r"disposizioni\s+sulla\s+legge\s+in\s+generale"],
    "cpc": [r"codice\s+di\s+procedura\s+civile"],
    "cc": [r"codice\s+civile"],
    "cp": [r"codice\s+penale"],
    "cds": [r"(?:nuovo\s+)?codice\s+della\s+strada"],
    "cap": [r"codice\s+delle\s+assicurazioni\s+private"],
    "cod-consumo": [r"codice\s+del\s+consumo"],
    "ccii": [r"codice\s+della\s+crisi\s+d['’]impresa\s+e\s+dell['’]insolvenza"],
    "cad": [r"codice\s+dell['’]amministrazione\s+digitale"],
    "dlgs-196-2003": [r"codice\s+in\s+materia\s+di\s+protezione\s+dei\s+dati\s+personali"],
}
_RX_VERBO = re.compile(r"\b(?:e['’]|è|sono)\s+(?:sostituit|abrogat|inserit|aggiunt|soppress|modificat)\w*"
                       r"|sono\s+apportate\s+le\s+seguenti\s+modific", re.I)
_SUFF = r"(?:bis|ter|quater|quinquies|sexies|septies|octies|novies|decies|undecies|duodecies|terdecies)"
_RX_NUM_ART = re.compile(r"\b(\d{1,4}(?:[\s-]*" + _SUFF + r")?)\b", re.I)


def _pattern_urn(urn: str):
    m = re.match(r"urn:nir:[^:]+:([^:]+):(\d{4})-(\d{2})-(\d{2});(\d+)", str(urn or ""))
    if not m or m.group(1) not in _TIPI_URN:
        return None
    tipo, anno, mese, giorno, num = m.groups()
    data = rf"{int(giorno)}(?:°|º)?\s+{_MESI[int(mese) - 1]}\s+{anno}"
    return (rf"{_TIPI_URN[tipo]}\s+(?:{data},?\s+)?n\.\s*{num}\b(?:\s+del\s+{anno})?"
            rf"|{_TIPI_URN[tipo]}\s+n\.\s*{num}\s+del\s+{anno}")


def pattern_atti(codici: dict = None) -> list:
    """[(slug, regex compilata)] in ordine di precedenza (nomi lunghi prima)."""
    codici = codici if codici is not None else cl.CODICI
    out = []
    for slug in sorted(codici, key=lambda s: (0 if s in _NOMI and "disp" in s else 1 if s in _NOMI else 2, s)):
        alternative = list(_NOMI.get(slug, []))
        pu = _pattern_urn(codici[slug].get("urn", ""))
        if pu and slug not in ("cc", "preleggi", "cpc", "disp-att-cpc", "disp-att-cc", "cp"):
            alternative.append(pu)       # i codici si citano per nome; le leggi per estremi
        if alternative:
            out.append((slug, re.compile("(?:" + "|".join(alternative) + ")", re.I)))
    return out


def _testo_html(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " ".join(unescape(t).split())


def modifiche_dirette(testo: str, patterns: list = None) -> dict:
    """{slug: {"articoli": [...], "tutto": bool}} degli atti osservati che il testo MODIFICA direttamente.
    `tutto` = formula B (elenco di modificazioni): l'elenco degli articoli puo' essere incompleto, quindi
    chi legge tratta in movimento l'intero atto.

    Le formule della tecnica legislativa per le novelle, e solo quelle:
      A) «All'articolo N [, comma M,] del <atto> … e' sostituito/abrogato/inserito …» (verbo entro 250
         caratteri; una citazione che sta DENTRO un virgolettato «…» e' testo nuovo, non la novella);
      B) «Al <atto> … sono apportate le seguenti modificazioni: a) all'articolo N …»;
      C) «L'articolo N del <atto> e' abrogato / e' sostituito dal seguente».
    Una citazione dell'atto senza formula di modifica (deroghe, rinvii) non conta. Un nome contenuto in
    uno piu' lungo (il c.p.c. dentro «disposizioni per l'attuazione del c.p.c.») non conta due volte.
    """
    patterns = patterns or pattern_atti()
    t = " ".join(str(testo or "").split())
    occupati, occorrenze = [], []
    for slug, rx in patterns:                      # precedenza: nomi lunghi prima
        for m in rx.finditer(t):
            if any(m.start() < b and m.end() > a for a, b in occupati):
                continue
            occupati.append((m.start(), m.end()))
            occorrenze.append((slug, m))
    trovati = {}
    for slug, m in sorted(occorrenze, key=lambda x: x[1].start()):
        if t.count("«", 0, m.start()) > t.count("»", 0, m.start()):
            continue                                   # dentro il testo nuovo virgolettato: non e' la novella
        prima = t[max(0, m.start() - 160):m.start()]
        dopo = t[m.end():m.end() + 400]
        dopo_fino_virgolette = dopo
        art = None
        ma = re.search(r"(?:^|[\s.;:(])(?:all['’]|agli\s+|nell['’])\s*articol[oi]\s+([^.;:«]{1,80}?)\s+"
                       r"(?:del(?:la|le|lo|l['’])?|al(?:la|le|lo|l['’])?)\s*$", prima, re.I)
        mc = re.search(r"(?:^|[\s.;:(])(?:l['’]|gli\s+)articol[oi]\s+([^.;:«]{1,60}?)\s+del(?:la|le|lo|l['’])?\s*$",
                       prima, re.I)
        if ma and _RX_VERBO.search(dopo_fino_virgolette[:250]):
            art = ma.group(1)
        elif mc and _RX_VERBO.search(dopo_fino_virgolette[:80]):
            art = mc.group(1)
        elif re.search(r"(?:^|\W)(?:al|alla|all['’]|ai|alle)\s*$", prima[-12:], re.I) and \
                re.search(r"sono\s+apportate\s+le\s+seguenti\s+modific", dopo[:300], re.I):
            coda = dopo[:4000]
            voce = trovati.setdefault(slug, {"articoli": [], "tutto": False})
            voce["tutto"] = True
            for x in re.findall(r"all['’]articolo\s+(\d{1,4}(?:[\s-]*" + _SUFF + r")?)", coda, re.I):
                x = re.sub(r"\s+", "-", x).lower()
                if x not in voce["articoli"]:
                    voce["articoli"].append(x)
            continue
        if art is None:
            continue
        voce = trovati.setdefault(slug, {"articoli": [], "tutto": False})
        for x in _RX_NUM_ART.findall(art.split(",")[0]):
            x = re.sub(r"[\s-]+", "-", x).lower()
            if x not in voce["articoli"]:
                voce["articoli"].append(x)
    return trovati


def _get_testo(url: str, timeout: float = 30) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def numeri_gu(html_30giorni: str) -> list:
    """[(data, numero)] dei numeri della Serie Generale elencati nella pagina «ultimi 30 giorni»."""
    visti, out = set(), []
    for d, n in re.findall(r"caricaDettaglio\?dataPubblicazioneGazzetta=(\d{4}-\d{2}-\d{2})&(?:amp;)?numeroGazzetta=(\d+)",
                           html_30giorni or ""):
        if (d, n) not in visti:
            visti.add((d, n))
            out.append((d, n))
    return out


def atti_normativi_sommario(html_sommario: str) -> list:
    """[{codice, data, titolo}] degli atti con codice redazionale «G» (leggi, decreti) di un numero."""
    voci, ordine = {}, []
    rx = (r'<a\b[^>]*href="[^"]*caricaDettaglioAtto/originario\?atto\.dataPubblicazioneGazzetta=(\d{4}-\d{2}-\d{2})'
          r'&(?:amp;)?atto\.codiceRedazionale=(\w+)[^"]*"[^>]*>(.*?)</a>')
    for data, cod, anchor in re.findall(rx, html_sommario or "", re.S):
        if not re.fullmatch(r"\d{2}G\d{5}", cod):
            continue
        testo = _testo_html(anchor)
        if cod not in voci:
            voci[cod] = {"codice": cod, "data": data, "titolo": testo}
            ordine.append(cod)
        elif testo and len(voci[cod]["titolo"]) < 200:
            voci[cod]["titolo"] = (voci[cod]["titolo"] + " — " + testo).strip(" —")
    return [voci[c] for c in ordine]


def articoli_menu(html_menu: str) -> list:
    """URL assoluti degli articoli (caricaArticolo) dal menu di un atto, senza doppioni, note escluse."""
    out, visti = [], set()
    for href, anchor in re.findall(r'<a\b[^>]*href="([^"]*caricaArticolo\?[^"]*)"[^>]*>(.*?)</a>', html_menu or "", re.S):
        href = unescape(href).split("#")[0]
        if href in visti or re.search(r"\bnote\b", _testo_html(anchor), re.I):
            continue
        visti.add(href)
        out.append(href if href.startswith("http") else GU + (href if href.startswith("/") else "/" + href))
    return out


def scansiona_gu(dal: _dt.date, al: _dt.date, get=None, patterns=None, massimo_atti: int = 60) -> dict:
    """Atti osservati modificati da leggi pubblicate in G.U. fra `dal` e `al`: {slug: [voce]} + esito."""
    get = get or _get_testo
    patterns = patterns or pattern_atti()
    esito = {"fonte": "ok", "numeri": 0, "atti_normativi": 0, "movimento": {}}
    try:
        numeri = [(d, n) for d, n in numeri_gu(get(f"{GU}/30giorni/serie_generale"))
                  if dal.isoformat() <= d <= al.isoformat()]
    except Exception as e:
        esito["fonte"] = f"errore: {e.__class__.__name__}"
        return esito
    esito["numeri"] = len(numeri)
    atti = []
    for d, n in numeri:
        try:
            atti += atti_normativi_sommario(get(f"{GU}/gazzetta/serie_generale/caricaDettaglio?"
                                                f"dataPubblicazioneGazzetta={d}&numeroGazzetta={n}"))
        except Exception:
            esito["fonte"] = "parziale"
    esito["atti_normativi"] = len(atti)
    for atto in atti[:massimo_atti]:
        url_menu = (f"{GU}/atto/vediMenuHTML?atto.dataPubblicazioneGazzetta={atto['data']}"
                    f"&atto.codiceRedazionale={atto['codice']}&tipoSerie=serie_generale&tipoVigenza=originario")
        try:
            testo = " ".join(_testo_html(get(u)) for u in articoli_menu(get(url_menu)))
        except Exception:
            esito["fonte"] = "parziale"
            continue
        for slug, voce in modifiche_dirette(testo, patterns).items():
            esito["movimento"].setdefault(slug, []).append({
                "codice_gu": atto["codice"], "data_gu": atto["data"], "titolo": atto["titolo"][:300],
                "articoli": voce["articoli"], "tutto_l_atto": voce["tutto"],
                "url": f"{GU}/eli/id/{atto['data'].replace('-', '/')}/{atto['codice']}/sg"})
    return esito


def aggiorna_movimento(movimento: dict, nuove: dict, indici: dict, oggi: _dt.date) -> dict:
    """Fonde le segnalazioni nuove e toglie quelle recepite dal consolidato (versioni >= data G.U.)."""
    mov = {k: dict(v) for k, v in (movimento or {}).items()}
    for slug, voci in (nuove or {}).items():
        entry = mov.setdefault(slug, {"dal": oggi.isoformat(), "atti_gu": [], "stato": "in_movimento"})
        noti = {x.get("codice_gu") for x in entry["atti_gu"]}
        for v in voci:
            if v["codice_gu"] not in noti:
                entry["atti_gu"].append(dict(v, rilevato_il=oggi.isoformat()))
    for slug in list(mov):
        meta = ((indici.get(slug) or {}).get("_meta") or {})
        consolidato = str(meta.get("consolidato_normattiva") or "")
        ultima = max([str(v.get("data") or "") for v in (meta.get("versioni") or [])] + [consolidato])
        entry = mov[slug]
        entry["atti_gu"] = [a for a in entry["atti_gu"] if not (ultima and ultima >= a.get("data_gu", "9999"))]
        if not entry["atti_gu"]:
            del mov[slug]
            continue
        piu_vecchio = min(a.get("rilevato_il") or oggi.isoformat() for a in entry["atti_gu"])
        if (oggi - _dt.date.fromisoformat(piu_vecchio)).days > MOVIMENTO_SCADENZA_GIORNI:
            entry["stato"] = "verifica_manuale"
    return mov


# ---------------------------------------------------------------- ricostruzione di un atto

def _scarica_con_backoff(slug: str, scarica, dormi, cache_urn: dict):
    urn = cl.CODICI[slug]["urn"]
    if urn in cache_urn:
        return cache_urn[urn], None
    errore = None
    for attesa in tuple(BACKOFF) + (None,):
        try:
            xml = scarica(slug)
            cache_urn[urn] = xml
            return xml, None
        except Exception as e:
            errore = f"{e.__class__.__name__}: {e}"
            if attesa is None:
                break
            dormi(attesa)
    return None, errore


def ricostruisci(slug: str, root: Path, *, forza: bool, scarica, dormi, cache_urn: dict, api_articoli=None) -> dict:
    """Riscarica un atto nel repo. Ritorna {esito, righe, meta|errore}. Senza differenze: tiene il vecchio."""
    testi = dir_testi(root)
    prev = Path(tempfile.mkdtemp(prefix=f"prev-{slug}-"))
    c_era = (testi / f"{slug}-indice.json").exists()
    if c_era:
        for nome in file_atto(slug, testi):
            if (testi / nome).exists():
                shutil.copy2(testi / nome, prev / nome)
    xml, errore = _scarica_con_backoff(slug, scarica, dormi, cache_urn)
    if xml is None:
        shutil.rmtree(prev, ignore_errors=True)
        return {"esito": "errore", "errore": errore or "download fallito"}
    try:
        meta = cl.costruisci(slug, xml, testi, datazione_api=True, fetch=api_articoli)
    except (SystemExit, Exception) as e:
        _ripristina(slug, prev, testi, c_era)
        shutil.rmtree(prev, ignore_errors=True)
        return {"esito": "errore", "errore": str(e) or e.__class__.__name__}
    righe = cd.confronta(slug, prev, testi) if c_era else []
    meta_prev = ((_leggi_json(prev / f"{slug}-indice.json", {}) or {}).get("_meta") or {})
    stesso_consolidato = str(meta_prev.get("consolidato_normattiva") or "") == str(meta.get("consolidato_normattiva") or "")
    if c_era and not righe and stesso_consolidato and not forza:
        _ripristina(slug, prev, testi, True)
        shutil.rmtree(prev, ignore_errors=True)
        return {"esito": "confermato_ricostruzione", "righe": [], "meta": meta_prev}
    shutil.rmtree(prev, ignore_errors=True)
    return {"esito": "ricostruito" if c_era else "nuovo", "righe": righe, "meta": meta}


def _ripristina(slug: str, prev: Path, testi: Path, c_era: bool) -> None:
    for vecchio in list(testi.glob(f"{slug}-*.md")) + list(testi.glob(f"{slug}.md")) + [testi / f"{slug}-indice.json"]:
        try:
            if vecchio.exists():
                vecchio.unlink()
        except OSError:
            pass
    if c_era:
        for p in prev.iterdir():
            shutil.copy2(p, testi / p.name)


# ---------------------------------------------------------------- manifest

def _articoli_in_movimento(entry: dict) -> list:
    """Articoli toccati da leggi non ancora consolidate; ["*"] = tutto l'atto (elenco non affidabile)."""
    atti_gu = entry.get("atti_gu") or []
    if any(a.get("tutto_l_atto") or not a.get("articoli") for a in atti_gu):
        return ["*"] if atti_gu else []
    return sorted({x for a in atti_gu for x in a.get("articoli") or []}, key=cl._ord_token)


def costruisci_manifest(root: Path, stato_atti: dict, fonti: dict, precedente: dict = None) -> dict:
    """Manifest dal disco: sha di ogni file + lo stato di verifica di ogni atto."""
    root = Path(root)
    testi = dir_testi(root)
    prec = (precedente or {}).get("atti") or {}
    mov = _leggi_json(percorso_movimento(root), {}) or {}
    atti = {}
    for slug in sorted(cl.CODICI):
        idx = _leggi_json(testi / f"{slug}-indice.json")
        if not idx:
            continue
        meta = idx.get("_meta") or {}
        voce_prec = prec.get(slug) or {}
        st = stato_atti.get(slug) or {}
        verificato = st.get("verificato_il") or voce_prec.get("verificato_il") or \
            max(str(meta.get("snapshot") or ""), str(meta.get("confermato_il") or ""))
        atti[slug] = {
            "snapshot": meta.get("snapshot"), "consolidato": meta.get("consolidato_normattiva"),
            "verificato_il": verificato, "esito": st.get("esito") or voce_prec.get("esito") or "iniziale",
            "articoli": meta.get("articoli"), "formato": int(meta.get("formato") or 1),
            "in_movimento": bool(mov.get(slug)),
            "articoli_in_movimento": _articoli_in_movimento(mov.get(slug) or {}),
            "file": {n: {"sha256": sha_file(testi / n), "byte": (testi / n).stat().st_size}
                     for n in file_atto(slug, testi) if (testi / n).exists()},
        }
    extra = {}
    for nome, p in (("codici", dir_normativa(root) / "codici.json"),
                    ("changelog", dir_normativa(root) / "changelog-auto.json"),
                    ("movimento", percorso_movimento(root))):
        if p.exists():
            extra[nome] = {"file": str(p.relative_to(root)).replace(os.sep, "/"), "sha256": sha_file(p), "byte": p.stat().st_size}
    cass = dir_normativa(root) / "cassazione"
    if cass.is_dir():
        extra["cassazione"] = {"file": {p.name: {"sha256": sha_file(p), "byte": p.stat().st_size}
                                        for p in sorted(cass.glob("*")) if p.is_file()},
                               **((_leggi_json(cass / "indice.json", {}) or {}).get("_meta") or {})}
    return {"schema": SCHEMA, "generato_il": _ora_utc(), "formato_indice": cl.FORMATO_INDICE,
            "repo": REPO_PUBBLICO, "fonti": fonti, "atti": atti, **extra}


def verifica(root: Path = ROOT, manifest: dict = None) -> list:
    """Errori bloccanti del corpus nel repo (lista vuota = pubblicabile)."""
    root = Path(root)
    testi = dir_testi(root)
    man = manifest if manifest is not None else _leggi_json(percorso_manifest(root))
    errori = []
    if not isinstance(man, dict) or man.get("schema") != SCHEMA:
        errori.append("manifest assente o di schema ignoto")
        man = {"atti": {}}
    for slug, cfg in sorted(cl.CODICI.items()):
        idx = _leggi_json(testi / f"{slug}-indice.json")
        if not idx:
            errori.append(f"{slug}: indice assente o illeggibile")
            continue
        meta = idx.get("_meta") or {}
        if int(meta.get("formato") or 1) != cl.FORMATO_INDICE:
            errori.append(f"{slug}: formato {meta.get('formato')} ≠ {cl.FORMATO_INDICE}")
        n = len(idx.get("articoli") or {})
        if n < int(cfg.get("min_articoli") or 1):
            errori.append(f"{slug}: {n} articoli, minimo {cfg.get('min_articoli')}")
        voce = (man.get("atti") or {}).get(slug)
        if not voce:
            errori.append(f"{slug}: manca nel manifest")
            continue
        for nome in file_atto(slug, testi):
            p = testi / nome
            if not p.exists():
                errori.append(f"{slug}: file {nome} mancante")
            elif (voce.get("file") or {}).get(nome, {}).get("sha256") != sha_file(p):
                errori.append(f"{slug}: sha di {nome} diverso dal manifest")
    return errori


# ---------------------------------------------------------------- il giro settimanale

def turno_rotazione(slugs: list, oggi: _dt.date) -> set:
    """Gli atti da ricostruire questa settimana a rotazione (settimana ISO modulo ROTAZIONE)."""
    settimana = oggi.isocalendar()[1] % ROTAZIONE
    return {s for i, s in enumerate(sorted(slugs)) if i % ROTAZIONE == settimana}


def settimanale(root: Path = ROOT, *, oggi: _dt.date = None, forza: bool = False, solo=None, post=None,
                scarica=None, get=None, dormi=time.sleep, api_articoli=None, con_gu: bool = True,
                rotazione: bool = True) -> dict:
    root = Path(root)
    oggi = oggi or _oggi()
    scarica = scarica or cl.scarica_akn
    man_prec = _leggi_json(percorso_manifest(root), {}) or {}
    atti_prec = man_prec.get("atti") or {}
    slugs = [s for s in (solo or sorted(cl.CODICI)) if s in cl.CODICI]
    verificati = [str((atti_prec.get(s) or {}).get("verificato_il") or "") for s in slugs]
    verificati = [v for v in verificati if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)]
    dal = min(_dt.date.fromisoformat(v) for v in verificati) if verificati else oggi - _dt.timedelta(days=8)
    dal = max(dal - _dt.timedelta(days=1), oggi - _dt.timedelta(days=FINESTRA_MAX_GIORNI))
    modificati, completo, dichiarati = atti_aggiornati(dal, oggi, post=post)
    fonti = {"aggiornati_api": "errore" if modificati is None else ("ok" if completo else "incompleta"),
             "finestra": [dal.isoformat(), oggi.isoformat()], "atti_dichiarati": dichiarati}
    esito = {"oggi": oggi.isoformat(), "fonti": fonti, "atti": {}, "righe_changelog": 0, "errori": {}}
    stato_atti, cache_urn, righe_tot = {}, {}, []
    for slug in slugs:
        cfg = cl.CODICI[slug]
        nuovo = not (dir_testi(root) / f"{slug}-indice.json").exists()
        oltre_finestra = False
        v_prec = str((atti_prec.get(slug) or {}).get("verificato_il") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v_prec):
            oltre_finestra = (oggi - _dt.date.fromisoformat(v_prec)).days > FINESTRA_MAX_GIORNI
        toccato = modificati is not None and chiave_urn(cfg["urn"]) in modificati
        di_turno = rotazione and slug in turno_rotazione(sorted(cl.CODICI), oggi)
        if forza or nuovo or toccato or oltre_finestra or di_turno:
            r = ricostruisci(slug, root, forza=forza, scarica=scarica, dormi=dormi, cache_urn=cache_urn,
                             api_articoli=api_articoli)
            if r["esito"] == "errore":
                esito["errori"][slug] = r["errore"]
                stato_atti[slug] = {"esito": "errore"}
                esito["atti"][slug] = "errore"
                continue
            righe_tot += r.get("righe") or []
            stato_atti[slug] = {"esito": r["esito"], "verificato_il": oggi.isoformat()}
        elif modificati is not None and completo:
            stato_atti[slug] = {"esito": "confermato_api", "verificato_il": oggi.isoformat()}
        else:
            stato_atti[slug] = {"esito": "non_verificato"}      # verificato_il resta quello di prima
        esito["atti"][slug] = stato_atti[slug]["esito"]
    if righe_tot:
        agg = cd.aggiorna_changelog(righe_tot, base=dir_normativa(root))
        esito["righe_changelog"] = agg.get("nuove", 0)
    # Gazzetta: atti in movimento
    indici = {s: _leggi_json(dir_testi(root) / f"{s}-indice.json", {}) for s in cl.CODICI}
    mov_prec = _leggi_json(percorso_movimento(root), {}) or {}
    nuove = {}
    if con_gu:
        gu = scansiona_gu(dal, oggi, get=get)
        fonti["gu"] = gu["fonte"]
        fonti["gu_atti_normativi"] = gu["atti_normativi"]
        nuove = gu["movimento"]
    mov = aggiorna_movimento(mov_prec, nuove, indici, oggi)
    if mov or percorso_movimento(root).exists():
        _scrivi_json(percorso_movimento(root), mov)
    esito["movimento"] = sorted(mov)
    man = costruisci_manifest(root, stato_atti, fonti, man_prec)
    errori = verifica(root, man)
    esito["verifica"] = errori
    if not errori:
        _scrivi_json(percorso_manifest(root), man)
    return esito


def rapporto_md(esito: dict) -> str:
    r = [f"## Corpus — giro del {esito.get('oggi')}", ""]
    f = esito.get("fonti") or {}
    r.append(f"- normattiva «atti aggiornati» {f.get('finestra')}: **{f.get('aggiornati_api')}** "
             f"({f.get('atti_dichiarati')} atti aggiornati nella finestra)")
    if "gu" in f:
        r.append(f"- Gazzetta: {f.get('gu')} · {f.get('gu_atti_normativi')} atti normativi letti")
    per_esito = {}
    for s, e in (esito.get("atti") or {}).items():
        per_esito.setdefault(e, []).append(s)
    for e, ss in sorted(per_esito.items()):
        r.append(f"- {e}: {len(ss)}" + (f" ({', '.join(ss)})" if e not in ("confermato_api",) else ""))
    r.append(f"- righe nuove nel changelog: {esito.get('righe_changelog', 0)}")
    if esito.get("movimento"):
        r.append(f"- in movimento: {', '.join(esito['movimento'])}")
    for s, e in (esito.get("errori") or {}).items():
        r.append(f"- ❌ {s}: {e}")
    if esito.get("verifica"):
        r.append("- ❌ **verifica fallita, manifest NON pubblicato**: " + "; ".join(esito["verifica"][:10]))
    return "\n".join(r) + "\n"


# ---------------------------------------------------------------- repo pubblico: nascita e manutenzione

README_PUBBLICO = """# Studio Coccolo — corpus normativo

Testi ufficiali degli atti normativi italiani usati dal plugin *Studio Coccolo*, come pubblicati da
[normattiva.it](https://www.normattiva.it) (formato Akoma Ntoso, testo vigente), con la data di vigenza
di ciascun articolo. I testi di legge non sono protetti dal diritto d'autore (art. 5 L. 633/1941).

- `wiki-studio/normativa/testi/` — un file `.md` per atto (o per libro) e il relativo indice `*-indice.json`
- `wiki-studio/normativa/changelog-auto.*` — modifiche articolo per articolo fra un aggiornamento e l'altro
- `wiki-studio/normativa/movimento.json` — atti modificati da leggi appena pubblicate in Gazzetta Ufficiale
  e non ancora recepite nel testo consolidato di normattiva
- `wiki-studio/normativa/cassazione/` — estremi dei provvedimenti civili della Corte di cassazione dal 2021
  (numero, sezione, date, tipo, materia; nessun dato personale), da SentenzeWeb
- `manifest.json` — impronte sha256 di ogni file e data dell'ultima verifica di ogni atto

Aggiornamento automatico ogni lunedì (`.github/workflows/settimanale.yml`). Non è una banca dati
ufficiale: per ogni uso professionale fa fede la fonte (normattiva, Gazzetta Ufficiale, SentenzeWeb).
"""

WORKFLOW = """name: corpus settimanale

on:
  schedule:
    - cron: "17 2 * * 1"          # lunedi' 04:17 ora italiana (02:17 UTC)
  workflow_dispatch:
    inputs:
      forza:
        description: "riscarica tutti gli atti"
        type: boolean
        default: false
      solo:
        description: "solo questi atti (slug separati da spazio)"
        type: string
        default: ""

permissions:
  contents: write

concurrency:
  group: corpus
  cancel-in-progress: false

jobs:
  aggiorna:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    env:
      STUDIO_STATO_ROOT: ${{ runner.temp }}/stato
      STUDIO_CORPUS: ${{ runner.temp }}/corpus-vivo
      PYTHONDONTWRITEBYTECODE: "1"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.9"
      - name: normattiva risponde?
        run: python3 scripts/corpus_pubblica.py --sonda
      - name: giro settimanale del corpus
        run: |
          ARGS="--settimanale --report $RUNNER_TEMP/rapporto.md"
          if [ "${{ inputs.forza }}" = "true" ]; then ARGS="$ARGS --forza"; fi
          for s in ${{ inputs.solo }}; do ARGS="$ARGS --solo $s"; done
          python3 scripts/corpus_pubblica.py $ARGS
      - name: estremi della Cassazione (anno in corso e precedente)
        continue-on-error: true
        run: python3 scripts/cassazione_indice.py --aggiorna --report $RUNNER_TEMP/cassazione.md
      - name: manifest finale e verifica
        run: python3 scripts/corpus_pubblica.py --manifest && python3 scripts/corpus_pubblica.py --verifica
      - name: pubblica
        run: |
          git config user.name "corpus-bot"
          git config user.email "corpus-bot@users.noreply.github.com"
          git add -A
          git commit -m "corpus $(date -u +%F)" || echo "nessuna modifica"
          git push
      - name: rapporto
        if: always()
        run: cat $RUNNER_TEMP/rapporto.md $RUNNER_TEMP/cassazione.md >> $GITHUB_STEP_SUMMARY 2>/dev/null || true
"""


def aggiorna_strumenti(repo: Path) -> list:
    repo = Path(repo)
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    copiati = []
    for nome in STRUMENTI:
        src = ROOT / "scripts" / nome
        if src.exists():
            shutil.copy2(src, repo / "scripts" / nome)
            copiati.append(nome)
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "settimanale.yml").write_text(WORKFLOW, encoding="utf-8")
    (repo / "README.md").write_text(README_PUBBLICO, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.DS_Store\n*.tmp\n", encoding="utf-8")
    dir_normativa(repo).mkdir(parents=True, exist_ok=True)
    shutil.copy2(dir_normativa() / "codici.json", dir_normativa(repo) / "codici.json")   # gli atti li decide il plugin
    try:
        ver = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        ver = None
    (repo / "scripts" / "VERSIONE").write_text(f"{ver or '?'}\n", encoding="utf-8")
    return copiati


def init(repo: Path) -> dict:
    """Crea il repo pubblico dal plugin: codici.json, testi degli atti, changelog, strumenti, workflow."""
    repo = Path(repo)
    testi_dst = dir_testi(repo)
    testi_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dir_normativa() / "codici.json", dir_normativa(repo) / "codici.json")
    n = 0
    for slug in sorted(cl.CODICI):
        for nome in file_atto(slug, dir_testi()):
            src = dir_testi() / nome
            if src.exists():
                shutil.copy2(src, testi_dst / nome)
                n += 1
    copiati = aggiorna_strumenti(repo)
    man = costruisci_manifest(repo, {}, {"aggiornati_api": "iniziale", "origine": "bundle del plugin"})
    _scrivi_json(percorso_manifest(repo), man)
    return {"file_testi": n, "strumenti": copiati, "atti": len(man["atti"]), "errori": verifica(repo, man)}


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")


def pubblica(repo: Path, forza: bool = False, solo=None, con_gu: bool = True) -> dict:
    """Riserva dal Mac: pull, strumenti e codici.json aggiornati dal plugin, giro settimanale con gli script
    DEL REPO PUBBLICO (cosi' codici.json e percorsi sono i suoi), commit e push."""
    repo = Path(repo)
    _git(repo, "pull", "--ff-only")
    aggiorna_strumenti(repo)
    cmd = [sys.executable, str(repo / "scripts" / "corpus_pubblica.py"), "--settimanale", "--json"]
    cmd += ["--forza"] if forza else []
    cmd += [x for s_ in (solo or []) for x in ("--solo", s_)]
    cmd += [] if con_gu else ["--senza-gu"]
    env = dict(os.environ)
    env["STUDIO_STATO_ROOT"] = tempfile.mkdtemp(prefix="corpus-pubblica-")
    env["STUDIO_CORPUS"] = str(Path(env["STUDIO_STATO_ROOT"]) / "corpus-vivo")
    r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", env=env)
    try:
        esito = json.loads(r.stdout)
    except ValueError:
        return {"verifica": [f"giro fallito: {(r.stderr or r.stdout)[-400:]}"], "pubblicato": False}
    if esito.get("verifica"):
        return {**esito, "pubblicato": False}
    _git(repo, "add", "-A")
    c = _git(repo, "commit", "-m", f"corpus {_oggi().isoformat()} (dal Mac)")
    p = _git(repo, "push")
    return {**esito, "pubblicato": p.returncode == 0, "git": (c.stdout + p.stderr)[-400:]}


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Costruttore del corpus pubblico (GitHub Action o Mac).")
    ap.add_argument("--sonda", action="store_true", help="exit 3 se normattiva non risponde")
    ap.add_argument("--settimanale", action="store_true")
    ap.add_argument("--forza", action="store_true")
    ap.add_argument("--solo", action="append", metavar="SLUG")
    ap.add_argument("--senza-gu", action="store_true", help="salta la lettura della Gazzetta")
    ap.add_argument("--report", default="", help="scrivi il rapporto markdown in questo file")
    ap.add_argument("--manifest", action="store_true", help="ricalcola il manifest dal disco (dopo passi esterni)")
    ap.add_argument("--verifica", action="store_true")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--aggiorna-strumenti", action="store_true")
    ap.add_argument("--pubblica", action="store_true")
    ap.add_argument("--repo", default="", help="cartella del repo pubblico (default: la radice di questo script)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve() if a.repo else ROOT

    if a.sonda:
        ok = sonda()
        print("normattiva: " + ("raggiungibile" if ok else "NON raggiungibile"))
        return 0 if ok else 3
    if a.init:
        res = init(repo)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if not res["errori"] else 1
    if a.aggiorna_strumenti:
        print("copiati: " + ", ".join(aggiorna_strumenti(repo)))
        return 0
    if a.manifest:
        prec = _leggi_json(percorso_manifest(repo), {}) or {}
        man = costruisci_manifest(repo, {}, prec.get("fonti") or {}, prec)
        _scrivi_json(percorso_manifest(repo), man)
        print(f"manifest: {len(man['atti'])} atti")
        return 0
    if a.verifica:
        errori = verifica(repo)
        print("verifica: " + ("OK" if not errori else f"{len(errori)} errori\n  " + "\n  ".join(errori)))
        return 0 if not errori else 1
    if a.settimanale or a.pubblica:
        kw = {"forza": a.forza, "solo": a.solo, "con_gu": not a.senza_gu}
        esito = pubblica(repo, **kw) if a.pubblica else settimanale(repo, **kw)
        md = rapporto_md(esito)
        if a.report:
            Path(a.report).write_text(md, encoding="utf-8")
        print(json.dumps(esito, ensure_ascii=False, indent=1) if a.json else md)
        return 0 if not esito.get("verifica") else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
