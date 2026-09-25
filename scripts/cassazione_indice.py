#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Indice degli estremi della Cassazione civile (piano v0.30): esistenza di una pronuncia in millisecondi.

COSA CONTIENE
-------------
Per ogni provvedimento civile indicizzato da SentenzeWeb (copertura dal 2021): numero, sezione, tipo,
data di decisione, data di deposito, materia. Niente testo, niente nomi (ne' parti ne' magistrati).
Un file per anno, `snciv-<anno>.tsv.gz`, ordinato e compresso in modo deterministico (stesso contenuto
= stesso sha: le settimane senza novita' non producono commit), piu' `indice.json` con le date.

COME SI USA
-----------
* lo costruisce la GitHub Action del corpus pubblico (`--aggiorna`: anno in corso e precedente; `--completo`
  una volta sola per gli anni chiusi) o, di riserva, il Mac;
* il plugin lo riceve con `corpus_sync.py` e `cassazione_locale.cerca()` lo consulta prima della rete:
  se la pronuncia c'e', gli estremi sono confermati senza chiamate; se NON c'e', si va comunque su
  SentenzeWeb — l'indice puo' essere indietro di una settimana e un'assenza non prova niente.

Uso:
  python3 scripts/cassazione_indice.py --aggiorna [--report FILE]
  python3 scripts/cassazione_indice.py --completo
  python3 scripts/cassazione_indice.py --cerca 24825 --anno 2026 [--sezione U]
Solo stdlib, Python 3.9.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if sys.platform == "win32":
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

ROOT = Path(__file__).resolve().parents[1]
SOLR = "https://www.italgiure.giustizia.it/sncass/isapi/hc.dll/sn.solr/sn-collection/select"
UA = "Mozilla/5.0 (StudioCoccolo cassazione_indice)"
COPERTURA_DA = 2021
PAGINA = 5000
CAMPI = "id,numdec,szdec,datdec,datdep,tipoprov,materia"
TIPI = {"Sentenza": "S", "Ordinanza": "O", "Ordinanza Interlocutoria": "OI", "Decreto": "D"}
TIPI_INV = {v: k for k, v in TIPI.items()}


def dir_seed(root: Path = ROOT) -> Path:
    return Path(root) / "wiki-studio" / "normativa" / "cassazione"


def dir_runtime() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from paths import stato_root  # noqa: E402
        return stato_root() / "normativa" / "cassazione"
    except Exception:
        return dir_seed()


# ---------------------------------------------------------------- scarico da SentenzeWeb

def _get_json(params: dict, timeout: float = 90) -> dict:
    url = SOLR + "?" + urlencode({"wt": "json", **params})
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e) and not isinstance(e, ssl.SSLError):
            raise
    # la catena TLS di italgiure non si verifica con il Python di sistema: stesso ripiego di cassazione_locale
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _data8(v) -> str:
    s = str(v[0] if isinstance(v, list) and v else v or "")
    return s if len(s) == 8 and s.isdigit() else ""


def riga(doc: dict):
    nd = str(doc.get("numdec") or "").strip()
    if not nd.isdigit():
        return None
    materia = doc.get("materia") or []
    materia = materia if isinstance(materia, list) else [materia]
    return (int(nd), str(doc.get("szdec") or ""), TIPI.get(str(doc.get("tipoprov") or ""), "?"),
            _data8(doc.get("datdec")), _data8(doc.get("datdep")),
            "; ".join(" ".join(str(m).split()) for m in materia if m))


def scarica_anno(anno: int, get=None, dormi=time.sleep) -> list:
    """Tutte le righe di un anno (kind snciv), paginando in ordine di id."""
    get = get or _get_json
    righe, start, totale = [], 0, None
    while totale is None or start < totale:
        d = None
        for attesa in (2, 8, 20, None):
            try:
                d = get({"q": f"kind:snciv AND anno:{anno}", "rows": PAGINA, "start": start, "sort": "id asc", "fl": CAMPI})
                break
            except Exception:
                if attesa is None:
                    raise
                dormi(attesa)
        resp = d.get("response") or {}
        totale = int(resp.get("numFound") or 0)
        docs = resp.get("docs") or []
        if not docs:
            break
        righe += [r for r in (riga(x) for x in docs) if r]
        start += len(docs)
    righe = sorted(set(righe))
    if totale and len(righe) < totale * 0.98:
        raise RuntimeError(f"anno {anno}: {len(righe)} righe su {totale} dichiarate — scarico incompleto, non scrivo")
    return righe


# ---------------------------------------------------------------- scrittura deterministica

def _gz(testo: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as g:
        g.write(testo.encode("utf-8"))
    return buf.getvalue()


def scrivi_anno(base: Path, anno: int, righe: list) -> dict:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    testo = "".join("\t".join(str(c) for c in r) + "\n" for r in righe)
    dati = _gz(testo)
    p = base / f"snciv-{anno}.tsv.gz"
    cambiato = not p.exists() or p.read_bytes() != dati
    if cambiato:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(dati)
        os.replace(str(tmp), str(p))
    return {"righe": len(righe), "sha256": hashlib.sha256(dati).hexdigest(), "byte": len(dati), "cambiato": cambiato}


def _indice(base: Path) -> dict:
    try:
        return json.loads((Path(base) / "indice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_meta": {}, "anni": {}}


def aggiorna(base: Path = None, anni=None, get=None, oggi: _dt.date = None) -> dict:
    base = Path(base) if base else dir_seed()
    oggi = oggi or _dt.date.today()
    anni = list(anni) if anni else [oggi.year - 1, oggi.year]
    ind = _indice(base)
    esito = {"anni": {}, "errori": {}}
    for anno in anni:
        try:
            righe = scarica_anno(anno, get=get)
        except Exception as e:
            esito["errori"][str(anno)] = f"{e.__class__.__name__}: {e}"
            continue
        info = scrivi_anno(base, anno, righe)
        voce = ind.setdefault("anni", {}).setdefault(str(anno), {})
        voce.update({"righe": info["righe"], "sha256": info["sha256"], "verificato_il": oggi.isoformat()})
        if info["cambiato"] or "aggiornato_il" not in voce:
            voce["aggiornato_il"] = oggi.isoformat()
        esito["anni"][str(anno)] = {k: info[k] for k in ("righe", "cambiato")}
    ind["_meta"] = {"fonte": "SentenzeWeb — Corte suprema di cassazione (italgiure.giustizia.it), indice Solr",
                    "copertura_da": COPERTURA_DA, "kind": "snciv",
                    "colonne": ["numdec", "sezione", "tipo", "data_decisione", "data_deposito", "materia"],
                    "tipi": TIPI_INV, "verificato_il": oggi.isoformat() if not esito["errori"] else
                    ind.get("_meta", {}).get("verificato_il")}
    (base / "indice.json").write_text(json.dumps(ind, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    esito["verificato_il"] = ind["_meta"]["verificato_il"]
    return esito


# ---------------------------------------------------------------- lettura (il plugin)

_CACHE = {}
_SEZIONI = {"UN": "U", "UNITE": "U", "SU": "U", "SS.UU": "U", "S.U": "U", "LAV": "L", "LAVORO": "L",
            "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6", "VII": "7"}


def _base_per_anno(anno: int, dirs=None):
    """La cartella con il file dell'anno piu' aggiornato fra runtime (sincronizzato) e seed del bundle."""
    migliore = None
    for b in (dirs or [dir_runtime(), dir_seed()]):
        b = Path(b)
        if not (b / f"snciv-{anno}.tsv.gz").exists():
            continue
        v = (_indice(b).get("anni") or {}).get(str(anno)) or {}
        chiave = str(v.get("verificato_il") or "")
        if migliore is None or chiave > migliore[0]:
            migliore = (chiave, b)
    return migliore[1] if migliore else None


def carica_anno(anno: int, dirs=None) -> dict:
    """{numdec: [righe]} di un anno, con cache per processo (mtime)."""
    b = _base_per_anno(anno, dirs)
    if b is None:
        return {}
    p = b / f"snciv-{anno}.tsv.gz"
    chiave = (str(p), p.stat().st_mtime)
    if _CACHE.get(anno, (None,))[0] == chiave:
        return _CACHE[anno][1]
    out = {}
    with gzip.open(p, "rt", encoding="utf-8") as f:
        for linea in f:
            c = linea.rstrip("\n").split("\t")
            if len(c) >= 5 and c[0].isdigit():
                out.setdefault(int(c[0]), []).append(c)
    _CACHE[anno] = (chiave, out)
    return out


def cerca(numero, anno, sezione=None, dirs=None) -> dict:
    """{"trovata": bool, "pronunce": [...], "indice_verificato_il": ..., "copertura": bool}.

    Un «trovata: False» NON dice che la pronuncia non esiste: dice solo che l'indice non la ha.
    """
    try:
        numero, anno = int(str(numero).strip()), int(str(anno).strip())
    except ValueError:
        return {"trovata": False, "pronunce": [], "copertura": False}
    if anno < COPERTURA_DA:
        return {"trovata": False, "pronunce": [], "copertura": False}
    b = _base_per_anno(anno, dirs)
    if b is None:
        return {"trovata": False, "pronunce": [], "copertura": False}
    righe = carica_anno(anno, dirs).get(numero, [])
    if sezione:
        sez = _SEZIONI.get(str(sezione).strip().upper().rstrip("."), str(sezione).strip().upper())
        filtrate = [r for r in righe if r[1].upper() == sez]
        righe = filtrate or righe
    v = (_indice(b).get("anni") or {}).get(str(anno)) or {}
    pronunce = [{"numero": str(int(r[0])), "anno": str(anno), "sezione": r[1] or None,
                 "tipo": TIPI_INV.get(r[2], r[2]), "data_decisione": _iso(r[3]), "data_deposito": _iso(r[4]),
                 "materia": [m for m in (r[5].split("; ") if len(r) > 5 else []) if m]} for r in righe]
    return {"trovata": bool(pronunce), "pronunce": pronunce, "copertura": True,
            "indice_verificato_il": v.get("verificato_il"), "fonte": str(b)}


def _iso(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s or "") == 8 else ""


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Indice degli estremi della Cassazione civile (SentenzeWeb, dal 2021).")
    ap.add_argument("--aggiorna", action="store_true", help="anno in corso e precedente")
    ap.add_argument("--completo", action="store_true", help=f"tutti gli anni dal {COPERTURA_DA}")
    ap.add_argument("--anno", type=int, action="append", help="solo questi anni (con --aggiorna)")
    ap.add_argument("--dir", default="", help="cartella di destinazione (default: wiki-studio/normativa/cassazione)")
    ap.add_argument("--cerca", default="", help="numero della pronuncia")
    ap.add_argument("--sezione", default="")
    ap.add_argument("--report", default="")
    a = ap.parse_args(argv)
    base = Path(a.dir) if a.dir else dir_seed()
    if a.cerca:
        anno = (a.anno or [None])[0]
        print(json.dumps(cerca(a.cerca, anno, a.sezione or None, dirs=[base] if a.dir else None), ensure_ascii=False, indent=1))
        return 0
    if a.aggiorna or a.completo:
        anni = a.anno or (range(COPERTURA_DA, _dt.date.today().year + 1) if a.completo else None)
        t0 = time.time()
        esito = aggiorna(base, anni)
        md = (f"## Cassazione — indice degli estremi\n\n- anni: "
              + ", ".join(f"{k}: {v['righe']} ({'aggiornato' if v['cambiato'] else 'invariato'})" for k, v in esito["anni"].items())
              + "".join(f"\n- ❌ {k}: {v}" for k, v in esito["errori"].items())
              + f"\n- durata: {round(time.time() - t0, 1)} s\n")
        if a.report:
            Path(a.report).write_text(md, encoding="utf-8")
        print(md)
        return 0 if not esito["errori"] else 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
