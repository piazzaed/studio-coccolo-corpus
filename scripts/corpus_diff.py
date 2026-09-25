#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diff del corpus normativo, articolo per articolo → changelog-auto (piano v0.25, D5.3).

COSA FA
-------
Confronta lo snapshot NUOVO di un atto (`<stato_root>/testi/`) con quello PRECEDENTE
(`<stato_root>/testi/precedente/`, archiviato da corpus_refresh.py prima di riscrivere) e registra
ogni articolo modificato, aggiunto, abrogato o rimosso in

    <stato_root>/normativa/changelog-auto.json   (macchina: lo legge il Livello 0 come lookup)
    <stato_root>/normativa/changelog-auto.md     (avvocato e Sentinella: drift-report gia' pronto)

Ogni riga: atto, articolo, vigore_da prima → dopo, atto modificante (dalle `versioni` dell'indice,
cioe' dal lifecycle AKN), data dei due snapshot. Idempotente: rieseguire lo stesso diff non duplica.

PERCHE' NON E' UNA FONTE
------------------------
Il changelog-auto dice CHE un articolo e' cambiato fra due date e per mano di quale atto; non dice
cosa significhi per la pratica. Nel Livello 0 entra come TRAPPOLA con gate intertemporale (come le
voci del changelog umano); la lettura del testo resta sul testo primario datato.

Uso:
  python3 scripts/corpus_diff.py --tutti                       # tutti gli atti con un precedente
  python3 scripts/corpus_diff.py --slug cpc --slug cc
  python3 scripts/corpus_diff.py --slug X --prima DIR --dopo DIR [--senza-scrivere] [--json]
  python3 scripts/corpus_diff.py --drift-report [--dal AAAA-MM-GG]   # sezione markdown per la Sentinella
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
if sys.platform == "win32":  # Cowork/Desktop su Windows: le pipe sono cp1252 → UTF-8
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codice_locale as cl   # noqa: E402
from paths import stato_root  # noqa: E402

TIPI = ("modificato", "aggiunto", "abrogato", "rimosso", "note", "datazione")
#: v0.30 — «note»: il testo normativo e' identico, sono cambiate le note redazionali di normattiva (spesso una
#: deroga o una disposizione transitoria nuova: si segnala); «datazione»: e' cambiato solo il vigore_da.
TIPI_TESTO = ("modificato", "aggiunto", "abrogato", "rimosso")


def dir_precedente() -> Path:
    return stato_root() / "testi" / "precedente"


def percorsi_changelog(base=None):
    """(json, md) del changelog-auto; `base` = cartella normativa alternativa (repo del corpus pubblico)."""
    base = Path(base) if base else stato_root() / "normativa"
    return base / "changelog-auto.json", base / "changelog-auto.md"


def _norm(t: str) -> str:
    return " ".join(str(t or "").split()).lower()


_RX_BLOCCO_NOTE = re.compile(r"\n\s*-{5,}\s*\n\s*AGGIORNAMENTO\b.*\Z", re.S)
_RX_RICHIAMO_NOTA = re.compile(r"\(\(\s*\d{1,3}\s*\)\)|\(\s*\d{1,3}\s*\)")


def solo_norma(testo: str) -> str:
    """Il testo normativo senza le note redazionali di normattiva: blocchi «AGGIORNAMENTO (N)», richiami
    «(N)»/«((N))» e le doppie parentesi che evidenziano le parti modificate (il testo dentro resta)."""
    t = _RX_BLOCCO_NOTE.sub("", "\n" + str(testo or ""))
    t = _RX_RICHIAMO_NOTA.sub(" ", t)
    return _norm(t.replace("((", "").replace("))", ""))


def _e_abrogato(voce: dict) -> bool:
    return bool(re.match(r"^\(?\(?\s*ARTICOLO\s+ABROGATO", (voce or {}).get("testo", "").strip(), re.I))


def _atto_modificante(versioni: list, vigore_da_dopo: str, cons_prima: str, cons_dopo: str):
    """L'atto che ha cambiato l'articolo: la versione con la stessa data del nuovo vigore_da; se non
    c'e', tutte le versioni entrate fra i due consolidati (un articolo puo' cambiare per piu' atti)."""
    if not versioni:
        return None
    for v in versioni:
        if vigore_da_dopo and v.get("data") == vigore_da_dopo and v.get("atto_modificante"):
            return v["atto_modificante"]
    finestra = [v for v in versioni if v.get("atto_modificante")
                and (not cons_prima or v["data"] > cons_prima) and (not cons_dopo or v["data"] <= cons_dopo)]
    atti = []
    for v in finestra:
        if v["atto_modificante"] not in atti:
            atti.append(v["atto_modificante"])
    return "; ".join(atti) or None


def confronta(slug: str, dir_prima: Path, dir_dopo: Path) -> list:
    """Le righe di modifica fra due snapshot dello stesso atto (vuota se uno dei due manca)."""
    meta_p, arts_p = cl.leggi_snapshot(Path(dir_prima), slug)
    meta_d, arts_d = cl.leggi_snapshot(Path(dir_dopo), slug)
    if not meta_p or not meta_d:
        return []
    if int(meta_p.get("formato") or 1) != int(meta_d.get("formato") or 1):
        # v0.29: snapshot di formati diversi (numeri dei commi, rubriche, date API): le differenze sarebbero
        # di FORMA, non di diritto. Si salta il confronto invece di riempire il changelog-auto di falsi.
        return []
    versioni = meta_d.get("versioni") or []
    cons_p, cons_d = str(meta_p.get("consolidato_normattiva") or ""), str(meta_d.get("consolidato_normattiva") or "")
    nome = meta_d.get("nome") or slug
    righe = []
    for token in sorted(set(arts_p) | set(arts_d), key=cl._ord_token):
        a, b = arts_p.get(token), arts_d.get(token)
        if a and not b:
            tipo = "rimosso"
        elif b and not a:
            tipo = "abrogato" if _e_abrogato(b) else "aggiunto"
        else:
            stessa_data = (a.get("vigore_da") or "") == (b.get("vigore_da") or "")
            if _norm(a["testo"]) == _norm(b["testo"]) and stessa_data:
                continue
            if solo_norma(a["testo"]) == solo_norma(b["testo"]):
                tipo = "datazione" if _norm(a["testo"]) == _norm(b["testo"]) else "note"
            else:
                tipo = "abrogato" if _e_abrogato(b) and not _e_abrogato(a) else "modificato"
        righe.append({
            "slug": slug, "atto": nome, "articolo": token,
            "rubrica": (b or a or {}).get("rubrica", ""), "tipo": tipo,
            "vigore_da_prima": (a or {}).get("vigore_da") or None,
            "vigore_da_dopo": (b or {}).get("vigore_da") or None,
            "atto_modificante": _atto_modificante(versioni, (b or {}).get("vigore_da"), cons_p, cons_d),
            "snapshot_prima": meta_p.get("snapshot"), "snapshot_dopo": meta_d.get("snapshot"),
            "consolidato_prima": cons_p or None, "consolidato_dopo": cons_d or None,
            "caratteri_prima": len((a or {}).get("testo", "")), "caratteri_dopo": len((b or {}).get("testo", "")),
        })
    return righe


# ---------------------------------------------------------------- changelog-auto (json + md)

def _chiave(r: dict) -> str:
    return f"{r.get('slug')}|{r.get('articolo')}|{r.get('snapshot_dopo')}|{r.get('tipo')}"


def leggi_changelog(base=None) -> list:
    pj, _ = percorsi_changelog(base)
    if not pj.exists():
        return []
    try:
        d = json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return list(d.get("modifiche") or []) if isinstance(d, dict) else []


def _render_md(modifiche: list) -> str:
    oggi = _dt.date.today().isoformat()
    r = ["# Changelog automatico del corpus normativo",
         "",
         f"<!-- generato da scripts/corpus_diff.py il {oggi} · {len(modifiche)} modifiche · file di DATI, non si modifica a mano -->",
         "",
         "> Differenze rilevate fra snapshot successivi degli atti del corpus locale (`codici.json`), articolo per",
         "> articolo. **Non è una fonte**: dice che un articolo è cambiato fra due date e per quale atto (dal",
         "> lifecycle dell'export Akoma Ntoso di normattiva). Il Livello 0 lo usa come trappola con gate",
         "> intertemporale; la Sentinella come drift-report già pronto; il testo si legge sempre sul primario datato.",
         ""]
    if not modifiche:
        r.append("_Nessuna modifica registrata._")
        return "\n".join(r) + "\n"
    per_atto = {}
    for m in modifiche:
        per_atto.setdefault((m.get("atto") or m.get("slug"), m.get("slug")), []).append(m)
    for (atto, slug), righe in sorted(per_atto.items(), key=lambda kv: max(x.get("snapshot_dopo") or "" for x in kv[1]), reverse=True):
        r.append(f"## {atto} (`{slug}`)")
        r.append("")
        r.append("| articolo | tipo | vigore_da prima → dopo | atto modificante | snapshot |")
        r.append("|---|---|---|---|---|")
        for m in sorted(righe, key=lambda x: (x.get("snapshot_dopo") or "", cl._ord_token(str(x.get("articolo") or ""))), reverse=True):
            rub = f" — {m['rubrica']}" if m.get("rubrica") else ""
            r.append(f"| Art. {m.get('articolo')}{rub} | {m.get('tipo')} | {m.get('vigore_da_prima') or '—'} → {m.get('vigore_da_dopo') or '—'} "
                     f"| {m.get('atto_modificante') or 'non indicato nell’AKN'} | {m.get('snapshot_prima') or '?'} → {m.get('snapshot_dopo') or '?'} |")
        r.append("")
    return "\n".join(r) + "\n"


def aggiorna_changelog(righe: list, base=None) -> dict:
    """Fonde `righe` nel changelog-auto (json + md). Ritorna {nuove, totale, json, md}."""
    pj, pm = percorsi_changelog(base)
    esistenti = leggi_changelog(base)
    chiavi = {_chiave(m) for m in esistenti}
    nuove = 0
    for r in righe:
        if _chiave(r) in chiavi:
            continue
        esistenti.append(dict(r, registrato_il=_dt.datetime.now().isoformat(timespec="seconds")))
        chiavi.add(_chiave(r))
        nuove += 1
    esistenti.sort(key=lambda m: (m.get("snapshot_dopo") or "", m.get("slug") or "", cl._ord_token(str(m.get("articolo") or ""))), reverse=True)
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps({"_meta": {"generato_il": _dt.datetime.now().isoformat(timespec="seconds"),
                                        "descrizione": "Modifiche del corpus locale rilevate da corpus_diff.py fra snapshot successivi. "
                                                       "Lookup del Livello 0 (verifica_meccanica.trappole_per) e drift-report della Sentinella.",
                                        "totale": len(esistenti)},
                              "modifiche": esistenti}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    pm.write_text(_render_md(esistenti), encoding="utf-8")
    _CACHE.clear()
    return {"nuove": nuove, "totale": len(esistenti), "json": str(pj), "md": str(pm)}


_CACHE = {}


def modifiche_per(slug: str, art_token: str) -> list:
    """Le righe del changelog-auto per un articolo (cache per mtime: il Livello 0 lo chiama per claim)."""
    pj, _ = percorsi_changelog()
    try:
        mtime = pj.stat().st_mtime if pj.exists() else None
    except OSError:
        mtime = None
    if _CACHE.get("mtime") != mtime or "righe" not in _CACHE:
        _CACHE["mtime"] = mtime
        _CACHE["righe"] = leggi_changelog() if mtime is not None else []
    tok = cl._norm_token("art. " + str(art_token))
    return [m for m in _CACHE["righe"] if m.get("slug") == slug and m.get("articolo") == tok]


def drift_report_sezione(dal: str = "", righe: list = None) -> str:
    """Sezione markdown per il drift-report della Sentinella: le modifiche dal `dal` (o tutte)."""
    modifiche = righe if righe is not None else leggi_changelog()
    if dal:
        modifiche = [m for m in modifiche if (m.get("snapshot_dopo") or "") >= dal]
    r = ["## CORPUS LOCALE — diff automatico degli snapshot (`changelog-auto.md`)", ""]
    if not modifiche:
        r.append(f"Nessuna modifica rilevata dal diff{' dal ' + dal if dal else ''}: snapshot rinnovati senza differenze per articolo.")
        return "\n".join(r) + "\n"
    r.append(f"{len(modifiche)} articoli cambiati{' dal ' + dal if dal else ''} (fonte: export AKN normattiva, confronto articolo per articolo). "
             "Ogni riga è una **trappola** già attiva nel Livello 0; qui va solo valutato se tocca voci di watchlist/changelog umano.")
    r.append("")
    r.append("| atto | articolo | tipo | vigore_da prima → dopo | atto modificante | snapshot |")
    r.append("|---|---|---|---|---|---|")
    for m in modifiche:
        r.append(f"| {m.get('atto')} | Art. {m.get('articolo')} | {m.get('tipo')} | {m.get('vigore_da_prima') or '—'} → {m.get('vigore_da_dopo') or '—'} "
                 f"| {m.get('atto_modificante') or 'non indicato'} | {m.get('snapshot_prima')} → {m.get('snapshot_dopo')} |")
    r.append("")
    r.append("Proposta: per ogni atto modificante non ancora nel `changelog-riforme.md`, aprire una voce (tell-only).")
    return "\n".join(r) + "\n"


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Diff articolo per articolo fra snapshot del corpus → changelog-auto.")
    ap.add_argument("--slug", action="append", help="atto (ripetibile)")
    ap.add_argument("--tutti", action="store_true", help="tutti gli atti che hanno uno snapshot precedente")
    ap.add_argument("--prima", help="cartella dello snapshot precedente (default <stato_root>/testi/precedente)")
    ap.add_argument("--dopo", help="cartella dello snapshot nuovo (default <stato_root>/testi)")
    ap.add_argument("--senza-scrivere", action="store_true", help="non aggiornare changelog-auto")
    ap.add_argument("--drift-report", action="store_true", help="stampa la sezione per il drift-report della Sentinella")
    ap.add_argument("--dal", default="", help="con --drift-report: solo le modifiche da questa data")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.drift_report:
        print(drift_report_sezione(a.dal))
        return 0
    prima = Path(a.prima) if a.prima else dir_precedente()
    dopo = Path(a.dopo) if a.dopo else cl._dir_runtime()
    slugs = list(a.slug or [])
    if a.tutti or not slugs:
        slugs = sorted({p.name[:-len("-indice.json")] for p in prima.glob("*-indice.json")}) if prima.exists() else []
    righe = []
    for slug in slugs:
        righe.extend(confronta(slug, prima, dopo))
    esito = {"slug": slugs, "righe": len(righe), "modifiche": righe}
    if not a.senza_scrivere:
        esito["changelog"] = aggiorna_changelog(righe)
    if a.json:
        print(json.dumps(esito, ensure_ascii=False, indent=1))
        return 0
    if not slugs:
        print("nessuno snapshot precedente da confrontare (corpus_refresh.py lo archivia al primo rinnovo)")
        return 0
    print(f"DIFF {', '.join(slugs)}: {len(righe)} articoli cambiati" + (f" · changelog-auto: +{esito['changelog']['nuove']} (totale {esito['changelog']['totale']})" if esito.get("changelog") else ""))
    for r in righe[:60]:
        print(f"  {r['slug']} art. {r['articolo']:<10} {r['tipo']:<10} {r['vigore_da_prima'] or '—'} → {r['vigore_da_dopo'] or '—'}"
              f"  {r.get('atto_modificante') or ''}")
    if len(righe) > 60:
        print(f"  … e altre {len(righe) - 60} (vedi changelog-auto.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
