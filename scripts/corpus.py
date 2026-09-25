#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Corpus vivo — la conoscenza dello Studio sopravvive alla reinstallazione del bundle.

IL PROBLEMA CHE RISOLVE
-----------------------
Fino alla v0.15 tutto ciò che il plugin impara veniva scritto **dentro** `wiki-studio/`, cioè
dentro la cartella del plugin installato. Quattro canali producono conoscenza nuova durante
l'uso normale:

  * la **Sentinella normativa** (drift-report, voci di changelog, watchlist);
  * **/debriefing-esito** (lezioni apprese, tattiche, prassi osservate su casi reali);
  * **/prassi-foro** (esperienza d'udienza dell'avvocato, tell-only);
  * **/scheda-foro** (schede di foro nuove o riverificate).

Ogni reinstallazione del `.plugin` sovrascrive quella cartella: **tutto quel lavoro sparisce**.
E poiché il pacchetto si aggiorna a mano al massimo ogni tre settimane, in mezzo c'erano tre
settimane di apprendimento che finivano nel cestino ad ogni giro — oppure, su un host che monta
il plugin in sola lettura, non venivano nemmeno scritte.

LA SOLUZIONE
------------
Le scritture non vanno più nella cartella del plugin: vanno in `~/.studio-coccolo/corpus/`,
che nessun bundle tocca (stessa scelta già fatta per il ledger e per la cache delle fonti).
Alla lettura, il contenuto spedito col bundle e quello locale vengono **fusi**:

    file del bundle (base)  +  voci locali datate  =  file VIVO

Il file vivo è materializzato in `~/.studio-coccolo/corpus/vivo/<percorso>` e la sua esistenza
viene segnalata dall'hook del router: le skill leggono quello invece dell'originale.

E IL GIRO SI CHIUDE
-------------------
Ogni tre settimane, prima di ricostruire il bundle, `--promuovi` travasa le voci locali nel
repo di sviluppo: entrano nei file spediti e vengono marcate con la versione di rilascio. Alla
reinstallazione il corpus vivo le riconosce come "già nel bundle" e **smette di ri-appenderle**,
tenendo solo ciò che è stato imparato dopo. Senza questo passo l'overlay crescerebbe per sempre
e duplicherebbe ogni voce ad ogni rilascio.

TIPI DI SOVRAPPOSIZIONE
-----------------------
* **aggiunta** (`--aggiungi`) — una voce datata in coda a un file esistente. È il caso normale:
  una lezione appresa, una prassi osservata, un contrasto giurisprudenziale. Non c'è conflitto
  possibile: se il bundle nuovo cambia la base, le voci si appendono lo stesso.
* **patch di un file-dati** (`--patch-json`) — su un `.json` non si può appendere testo senza
  distruggerlo: le voci locali sono patch dichiarative per chiave («nella lista `voci`, alla voce
  `cartabia_processo_civile`, `stato` = da_ricontrollare»), applicate sopra la base. Toccano solo
  i campi indicati, quindi continuano a valere anche quando il rilascio successivo riscrive il
  resto della voce. Con `--appendi` si aggiunge un elemento nuovo alla lista.
* **sostituzione** (`--sostituisci`) — un file intero (una scheda di foro nuova, un changelog
  riscritto dalla Sentinella). Qui il conflitto esiste: se il bundle nuovo porta una versione
  diversa della base, l'overlay la mascherebbe in silenzio. Per questo ogni sostituzione registra
  l'impronta della base al momento della scrittura e, se la base cambia, `--stato` lo dichiara
  come **DIVERGENZA** da risolvere a mano. Meglio una riga rumorosa che una regola vecchia
  applicata per mesi senza che nessuno se ne accorga.

CLI
---
    python3 scripts/corpus.py --stato [--json]
    python3 scripts/corpus.py --leggi prassi/foro-torino.md
    python3 scripts/corpus.py --aggiungi redazione/lezioni-apprese.md \
        --testo "..." --fonte "RG 123/2026 · udienza 12/05/2026" [--sezione "..."] [--data 2026-08-26]
    python3 scripts/corpus.py --sostituisci prassi/foro-milano.md --da /tmp/scheda.md
    python3 scripts/corpus.py --patch-json normativa/changelog-riforme.meta.json \
        --lista voci --chiave voce_id --id cartabia_processo_civile \
        --set '{"stato":"da_ricontrollare"}' --fonte "Sentinella 2026-08-26"
    python3 scripts/corpus.py --patch-json normativa/sentinella-debito.json --appendi \
        --lista debito --chiave id --set '{"id":42,"cosa":"…","stato":"aperto"}'
    python3 scripts/corpus.py --materializza [--forza]
    python3 scripts/corpus.py --elenco-vivi [--json]
    python3 scripts/corpus.py --promuovi [--dry-run] [--versione 0.16.0]
    python3 scripts/corpus.py --dove

Solo stdlib.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
if sys.platform == "win32":  # Cowork/Desktop su Windows: le pipe sono cp1252 → UTF-8 (accenti, frecce, emoji)
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT, WIKI  # noqa: E402

#: Marcatore che apre il blocco delle voci locali dentro un file materializzato.
APRI = "<!-- corpus-vivo:inizio -->"
CHIUDI = "<!-- corpus-vivo:fine -->"

#: File che NON possono essere sovrascritti dall'overlay: sono i gate del pavimento
#: anti-allucinazione. Il corpus vivo aggiunge conoscenza, non riscrive le regole di verifica.
#: (Vedi wiki-studio/protocolli/ — nucleo, lenti, datazione, struttura, intestazioni.)
PROTETTI = (
    "protocolli/nucleo-verifica.md",
    "protocolli/lenti-verifica.md",
    "protocolli/datazione-verifica.md",
    "protocolli/cascata-obbligatoria.md",
    "protocolli/intestazioni-atti.md",
    "redazione/deontologia-redazionale.md",
)

#: Percorsi che restano **per sempre** nel corpus e non vengono mai travasati nel repo.
#: I drift-report sono esclusi dal bundle per scelta (`crea_bundle.py`: sono la narrazione di un
#: giro, non il seed del plugin). Promuoverli li scriverebbe nel repo per poi lasciarli fuori dal
#: pacchetto: dopo l'installazione risulterebbero "già nel bundle" e sparirebbero dalla macchina
#: dell'avvocato. Tenendoli qui, il corpus diventa il loro archivio — e sopravvive ai rilasci.
SOLO_LOCALI = (
    "normativa/drift-reports/",
)


def solo_locale(rel: str) -> bool:
    return any(rel.startswith(p) for p in SOLO_LOCALI)


# ============================================================ percorsi

def corpus_dir() -> Path:
    env = os.environ.get("STUDIO_CORPUS")
    if env:
        return Path(env)
    from paths import stato_root
    return stato_root() / "corpus"


def _agg(rel: str) -> Path:
    return corpus_dir() / "agg" / (rel + ".jsonl")


def _sos(rel: str) -> Path:
    return corpus_dir() / "sos" / rel


def _vivo(rel: str) -> Path:
    return corpus_dir() / "vivo" / rel


def _stato_file() -> Path:
    return corpus_dir() / "stato.json"


def normalizza_rel(rel: str) -> str:
    """Accetta 'prassi/x.md', 'wiki-studio/prassi/x.md' o un assoluto dentro la wiki.

    Ritorna sempre il percorso relativo a `wiki-studio/`. Rifiuta le fughe dall'albero:
    un `..` in un argomento che arriva da una skill scriverebbe fuori dal corpus.
    """
    r = str(rel).strip().replace("\\", "/")
    # L'assolutezza si controlla PRIMA di togliere gli slash iniziali: altrimenti "/etc/passwd"
    # diventerebbe il relativo "etc/passwd" e passerebbe il controllo invece di essere respinto.
    if r.startswith("/") or Path(r).is_absolute():
        try:
            r = str(Path(r).resolve().relative_to(Path(WIKI).resolve()))
        except ValueError:
            raise SystemExit(f"[corpus] percorso fuori da wiki-studio: {rel}")
    if r.startswith("wiki-studio/"):
        r = r[len("wiki-studio/"):]
    if ".." in Path(r).parts or not r:
        raise SystemExit(f"[corpus] percorso non ammesso: {rel}")
    return r


def _base_path(rel: str) -> Path:
    return WIKI / rel


def _sha(testo: str) -> str:
    return hashlib.sha256(testo.encode("utf-8")).hexdigest()[:16]


# ============================================================ versione del plugin

def versione_plugin() -> str:
    try:
        j = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return str(j.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def _vtuple(v: str):
    nums = re.findall(r"\d+", str(v or "0"))
    return tuple(int(n) for n in (nums + ["0", "0", "0"])[:3])


def gia_nel_bundle(voce: dict, versione: str | None = None) -> bool:
    """True se la voce è stata promossa in una versione <= a quella installata.

    È il meccanismo che impedisce alla voce di comparire due volte dopo un rilascio: una
    nell'originale (perché `--promuovi` l'ha travasata nel file spedito) e una in coda
    (perché l'overlay continuava ad appenderla).
    """
    p = voce.get("promosso_in")
    if not p:
        return False
    return _vtuple(p) <= _vtuple(versione or versione_plugin())


# ============================================================ lettura

def _leggi_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for riga in p.read_text(encoding="utf-8").splitlines():
        riga = riga.strip()
        if not riga:
            continue
        try:
            out.append(json.loads(riga))
        except ValueError:
            continue  # una riga corrotta non deve far perdere le altre
    return out


def voci_attive(rel: str, versione: str | None = None) -> list[dict]:
    """Le voci locali che DEVONO comparire nel file vivo, in ordine di scrittura."""
    return [v for v in _leggi_jsonl(_agg(rel))
            if not v.get("ritirata") and not gia_nel_bundle(v, versione)]


def base_testo(rel: str) -> tuple[str, str]:
    """Ritorna (testo, provenienza) della base: la sostituzione locale, se c'è, o il bundle."""
    s = _sos(rel)
    if s.exists():
        return s.read_text(encoding="utf-8"), "locale"
    b = _base_path(rel)
    if b.exists():
        return b.read_text(encoding="utf-8"), "bundle"
    return "", "assente"


def _blocco_voci(rel: str, voci: list[dict]) -> str:
    """Compone il blocco delle voci locali, raggruppate per sezione dichiarata."""
    if not voci:
        return ""
    date = sorted(v.get("data", "") for v in voci if v.get("data"))
    intervallo = f"{date[0]} → {date[-1]}" if date else "senza data"
    righe = [
        "",
        APRI,
        f"## Voci locali — corpus vivo ({len(voci)} voci · {intervallo})",
        "",
        "> Scritte durante l'uso (Sentinella · /debriefing-esito · /prassi-foro · /scheda-foro)",
        "> **dopo** il bundle installato, e non ancora travasate in un rilascio. Valgono come le",
        "> voci del file sopra e portano ciascuna data e fonte. Le voci 🟡 restano tell-only:",
        "> utilizzabili in strategia, **mai citabili in atto come se fossero regole**.",
        "",
    ]
    per_sezione: dict[str, list[dict]] = {}
    for v in voci:
        per_sezione.setdefault(v.get("sezione") or "", []).append(v)
    for sez, gruppo in per_sezione.items():
        if sez:
            righe.append(f"### {sez}")
        for v in gruppo:
            tag = (v.get("tag") or "").strip()
            testa = " · ".join(x for x in [v.get("data", "s.d."),
                                           f"fonte: {v['fonte']}" if v.get("fonte") else ""] if x)
            righe.append(f"- [{testa}] {tag + ' ' if tag else ''}{v.get('testo', '').strip()}")
        righe.append("")
    righe.append(CHIUDI)
    return "\n".join(righe) + "\n"


def _avviso_divergenza(rel: str) -> str:
    d = divergenza(rel)
    if not d:
        return ""
    return (
        "\n> ⚠️ **DIVERGENZA** — questo file sostituisce integralmente quello del bundle, ma la\n"
        "> versione spedita è cambiata dopo la sostituzione. Il contenuto qui sotto è quello\n"
        "> locale: verificare che non stia mascherando un aggiornamento del rilascio\n"
        f"> (`python3 scripts/corpus.py --stato`, voce `{rel}`).\n\n"
    )


def _uguale(a, b) -> bool:
    """Confronto tollerante al tipo fra identificativi.

    Gli id arrivano dalla riga di comando come stringhe ("--id 2") ma nei file-dati possono
    essere numeri (`"id": 2`): un confronto secco non trova mai la voce e la patch si perde in
    silenzio — il modo peggiore di sbagliare, perché il comando risponde comunque «PATCH».
    """
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


def _fondi_json(base: str, voci: list[dict]) -> str:
    """Fusione per CHIAVE di un file-dati JSON (il sidecar della Sentinella).

    Su un .json non si può appendere un blocco di testo: il file smetterebbe di essere JSON e
    `freshness.py` morirebbe all'avvio. Le voci locali sono quindi patch dichiarative —
    «nella lista `voci`, alla voce con `voce_id` = X, imposta `stato` = da_ricontrollare» — e
    qui vengono applicate in ordine di scrittura sopra la base spedita col bundle.

    Perché per chiave e non per sostituzione: se il bundle nuovo porta una voce aggiornata,
    la patch continua a valere sul campo che tocca senza cancellare il resto della voce nuova.
    """
    try:
        dati = json.loads(base) if base.strip() else {}
    except ValueError:
        return base            # non è JSON valido: non peggioriamo la situazione
    applicate = 0
    for v in voci:
        p = v.get("patch")
        if not isinstance(p, dict):
            continue
        campi = p.get("set") or {}
        lista, chiave, ident = p.get("lista"), p.get("chiave"), p.get("id")
        if p.get("azione") == "append" and lista:
            # Voce NUOVA in una lista (tipico: un debito di verifica aperto dalla Sentinella).
            # Se una voce con la stessa chiave è già arrivata col bundle, la si aggiorna invece
            # di aggiungerne una seconda con lo stesso id.
            elenco = dati.setdefault(lista, [])
            esistente = next((e for e in elenco if isinstance(e, dict) and chiave
                              and _uguale(e.get(chiave), campi.get(chiave))), None)
            if esistente is not None:
                esistente.update(campi)
            else:
                elenco.append(campi)
            applicate += 1
        elif lista:
            for elem in (dati.get(lista) or []):
                if isinstance(elem, dict) and _uguale(elem.get(chiave), ident):
                    elem.update(campi)
                    applicate += 1
                    break
        elif isinstance(dati, dict):
            dati.update(campi)
            applicate += 1
    if applicate and isinstance(dati, dict):
        dati.setdefault("_meta", {})["corpus_vivo"] = (
            f"{applicate} patch locali applicate (scritte dopo il bundle installato; "
            "originale intatto in wiki-studio/, elenco con `corpus.py --stato`)")
    return json.dumps(dati, ensure_ascii=False, indent=1) + "\n"


def risolvi(rel: str, versione: str | None = None) -> str:
    """Il testo VIVO del file: base (locale o del bundle) + voci locali datate."""
    rel = normalizza_rel(rel)
    testo, prov = base_testo(rel)
    voci = voci_attive(rel, versione)
    if prov == "assente" and not voci:
        raise SystemExit(f"[corpus] nessuna base né voci locali per: {rel}")
    if rel.endswith(".json"):
        return _fondi_json(testo, voci)
    testa = _avviso_divergenza(rel) if prov == "locale" else ""
    return (testa + testo).rstrip("\n") + "\n" + _blocco_voci(rel, voci)


def patch_json(rel: str, campi: dict, lista: str = "", chiave: str = "", ident: str = "",
               fonte: str = "", data: str = "", azione: str = "set") -> dict:
    """Registra una patch su un file-dati JSON (vedi `_fondi_json`)."""
    rel = normalizza_rel(rel)
    if not rel.endswith(".json"):
        raise SystemExit(f"[corpus] --patch-json vale solo sui .json: {rel}")
    if not campi:
        raise SystemExit("[corpus] --set richiede almeno un campo")
    if azione == "append":
        if not lista:
            raise SystemExit("[corpus] --appendi richiede --lista")
        descrizione = f"{lista} += " + json.dumps(campi, ensure_ascii=False)[:200]
    else:
        descrizione = (f"{lista}[{chiave}={ident}] ← " if lista else "← ") + json.dumps(
            campi, ensure_ascii=False)
    voce = {
        "id": _sha(f"{rel}|{azione}|{lista}|{ident}|{json.dumps(campi, sort_keys=True)}"),
        "data": (data or _dt.date.today().isoformat()),
        "fonte": fonte.strip(),
        "sezione": "",
        "tag": "",
        "testo": descrizione,
        "patch": {"lista": lista, "chiave": chiave, "id": ident, "set": campi,
                  "azione": azione},
        "scritto_il": _dt.datetime.now().isoformat(timespec="seconds"),
        "promosso_in": None,
    }
    p = _agg(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    if voce["id"] in {v.get("id") for v in _leggi_jsonl(p)}:
        return {**voce, "duplicata": True}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(voce, ensure_ascii=False) + "\n")
    _invalida()
    return voce


def ha_contenuto_locale(rel: str) -> bool:
    rel = normalizza_rel(rel)
    return bool(voci_attive(rel)) or _sos(rel).exists()


def elenco_rel() -> list[str]:
    """Tutti i percorsi con contenuto locale, deduplicati e ordinati."""
    base = corpus_dir()
    trovati = set()
    agg = base / "agg"
    if agg.exists():
        for p in agg.rglob("*.jsonl"):
            trovati.add(str(p.relative_to(agg))[: -len(".jsonl")])
    sos = base / "sos"
    if sos.exists():
        for p in sos.rglob("*"):
            if p.is_file():
                trovati.add(str(p.relative_to(sos)))
    return sorted(r for r in trovati if ha_contenuto_locale(r))


def divergenza(rel: str) -> dict | None:
    """Per una sostituzione: la base del bundle è cambiata dopo la scrittura dell'overlay?"""
    rel = normalizza_rel(rel)
    if not _sos(rel).exists():
        return None
    st = _stato()
    reg = (st.get("sostituzioni") or {}).get(rel) or {}
    atteso = reg.get("base_hash")
    b = _base_path(rel)
    attuale = _sha(b.read_text(encoding="utf-8")) if b.exists() else None
    if atteso is None or attuale is None or atteso == attuale:
        return None
    return {"rel": rel, "base_hash_scritto": atteso, "base_hash_attuale": attuale,
            "scritto_il": reg.get("scritto_il", "")}


# ============================================================ stato

def _stato() -> dict:
    p = _stato_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _salva_stato(st: dict) -> None:
    p = _stato_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ============================================================ scrittura

def aggiungi(rel: str, testo: str, fonte: str = "", data: str = "",
             sezione: str = "", tag: str = "") -> dict:
    rel = normalizza_rel(rel)
    if not testo.strip():
        raise SystemExit("[corpus] --testo vuoto: una voce senza contenuto non si scrive")
    voce = {
        "id": _sha(f"{rel}|{testo}|{fonte}|{data}"),
        "data": (data or _dt.date.today().isoformat()),
        "fonte": fonte.strip(),
        "sezione": sezione.strip(),
        "tag": tag.strip(),
        "testo": testo.strip(),
        "scritto_il": _dt.datetime.now().isoformat(timespec="seconds"),
        "promosso_in": None,
    }
    p = _agg(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    esistenti = {v.get("id") for v in _leggi_jsonl(p)}
    if voce["id"] in esistenti:
        return {**voce, "duplicata": True}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(voce, ensure_ascii=False) + "\n")
    _invalida()
    return voce


def sostituisci(rel: str, contenuto: str, motivo: str = "") -> dict:
    rel = normalizza_rel(rel)
    if rel in PROTETTI:
        raise SystemExit(
            f"[corpus] {rel} è un file-gate del pavimento di verifica: non è sostituibile "
            "dall'overlay. Se va cambiato, si cambia nel repo e si rilascia un bundle.")
    p = _sos(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contenuto.rstrip("\n") + "\n", encoding="utf-8")
    b = _base_path(rel)
    st = _stato()
    st.setdefault("sostituzioni", {})[rel] = {
        "base_hash": _sha(b.read_text(encoding="utf-8")) if b.exists() else None,
        "scritto_il": _dt.datetime.now().isoformat(timespec="seconds"),
        "motivo": motivo,
        "nuovo": not b.exists(),
    }
    _salva_stato(st)
    _invalida()
    return {"rel": rel, "byte": len(contenuto), "nuovo": not b.exists()}


def _invalida() -> None:
    st = _stato()
    st["materializzato_il"] = None
    _salva_stato(st)


# ============================================================ materializzazione

def _impronta_sorgenti() -> str:
    """Impronta di tutto ciò che influenza i file vivi: overlay + basi + versione plugin."""
    pezzi = [versione_plugin()]
    for rel in elenco_rel():
        for p in (_agg(rel), _sos(rel), _base_path(rel)):
            if p.exists():
                pezzi.append(f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}")
    return _sha("|".join(pezzi))


def materializza(forza: bool = False) -> dict:
    """Scrive i file vivi. Idempotente e a costo ~zero se nulla è cambiato."""
    st = _stato()
    impronta = _impronta_sorgenti()
    # La versione entra nella condizione oltre che nell'impronta: se restasse fuori, uno
    # `stato.json` con la versione vecchia e l'impronta giusta terrebbe l'hook a chiedere in
    # eterno una rimaterializzazione che poi non farebbe niente.
    if (not forza and st.get("impronta") == impronta and st.get("materializzato_il")
            and st.get("versione_plugin") == versione_plugin()):
        return {"aggiornati": 0, "invariato": True, "file": st.get("file") or elenco_rel()}
    scritti = []
    for rel in elenco_rel():
        v = _vivo(rel)
        v.parent.mkdir(parents=True, exist_ok=True)
        v.write_text(risolvi(rel), encoding="utf-8")
        scritti.append(rel)
    # I file vivi rimasti orfani (voci tutte promosse, sostituzione rimossa) vanno tolti:
    # un file vivo che nessuno rigenera più è esattamente la conoscenza vecchia che il
    # corpus vivo esiste per evitare.
    radice = corpus_dir() / "vivo"
    if radice.exists():
        vivi = {str(p.relative_to(radice)) for p in radice.rglob("*") if p.is_file()}
        for orfano in vivi - set(scritti):
            try:
                (radice / orfano).unlink()
            except OSError:
                pass
    st["impronta"] = impronta
    st["materializzato_il"] = _dt.datetime.now().isoformat(timespec="seconds")
    st["versione_plugin"] = versione_plugin()
    st["file"] = scritti
    _salva_stato(st)
    return {"aggiornati": len(scritti), "invariato": False, "file": scritti}


# ============================================================ percorso veloce per l'hook

def per_hook() -> dict:
    """Ciò che serve al router, letto da UN solo file piccolo.

    L'hook gira a ogni prompt: qui non si può fare il giro dell'albero. Si legge `stato.json`
    (poche centinaia di byte) e si dice all'hook due cose: quali file vivi annunciare, e se la
    materializzazione è da rifare perché nel frattempo è stato installato un bundle nuovo.
    """
    try:
        st = _stato()
        file = st.get("file") or []
        scaduto = (st.get("versione_plugin") != versione_plugin()) or not st.get("materializzato_il")
        return {"file": file, "vivo_dir": str(corpus_dir() / "vivo"), "rimaterializza": scaduto}
    except Exception:
        return {"file": [], "vivo_dir": "", "rimaterializza": False}


# ============================================================ promozione

def promuovi(versione: str | None = None, dry_run: bool = False) -> dict:
    """Travasa l'overlay nei file del repo e marca le voci con la versione di rilascio.

    Si esegue **nel repo di sviluppo**, prima di `crea_bundle.py`. È il passo che chiude il
    ciclo: senza, l'overlay cresce all'infinito e dopo il rilascio ogni voce comparirebbe due
    volte (una nel file spedito, una in coda).
    """
    versione = versione or versione_plugin()
    esiti = {"versione": versione, "file": [], "voci": 0, "sostituzioni": 0,
             "divergenze": [], "saltati": [], "dry_run": dry_run}
    for rel in elenco_rel():
        if solo_locale(rel):
            esiti["saltati"].append(rel)     # archivio locale: non entra mai nel pacchetto
            continue
        d = divergenza(rel)
        if d:
            esiti["divergenze"].append(d)
        base = _base_path(rel)
        s = _sos(rel)
        voci = voci_attive(rel, versione="0.0.0")   # tutte quelle non ancora promosse
        nuovo = (s.read_text(encoding="utf-8") if s.exists()
                 else (base.read_text(encoding="utf-8") if base.exists() else ""))
        if rel.endswith(".json"):
            # Sui file-dati le patch si APPLICANO alla base: promuovere significa che il valore
            # locale diventa il valore spedito, non che si aggiunge una nota in coda.
            nuovo = _fondi_json(nuovo, voci)
        else:
            if voci:
                nuovo = nuovo.rstrip("\n") + "\n" + _blocco_voci(rel, voci)
            # Il blocco promosso diventa testo normale: i marcatori servono solo al file vivo.
            nuovo = nuovo.replace(APRI + "\n", "").replace(CHIUDI + "\n", "")
            nuovo = re.sub(r"^## Voci locali — corpus vivo .*$",
                           f"## Voci acquisite durante l'uso (fino alla v{versione})",
                           nuovo, flags=re.M)
        esiti["file"].append({"rel": rel, "voci": len(voci),
                              "sostituzione": s.exists(), "byte": len(nuovo)})
        esiti["voci"] += len(voci)
        esiti["sostituzioni"] += 1 if s.exists() else 0
        if dry_run:
            continue
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(nuovo, encoding="utf-8")
        # marca le voci come promosse e ritira la sostituzione: da qui in poi la base è il repo
        p = _agg(rel)
        if p.exists():
            righe = []
            for v in _leggi_jsonl(p):
                if not v.get("promosso_in"):
                    v["promosso_in"] = versione
                righe.append(json.dumps(v, ensure_ascii=False))
            p.write_text("\n".join(righe) + "\n", encoding="utf-8")
        if s.exists():
            s.unlink()
            st = _stato()
            (st.get("sostituzioni") or {}).pop(rel, None)
            _salva_stato(st)
    if not dry_run:
        materializza(forza=True)
    return esiti


# ============================================================ report

def stato_report() -> dict:
    rel_list = elenco_rel()
    voci_tot = sum(len(voci_attive(r)) for r in rel_list)
    div = [d for d in (divergenza(r) for r in rel_list) if d]
    st = _stato()
    return {
        "corpus": str(corpus_dir()),
        "versione_plugin": versione_plugin(),
        "file_vivi": len(rel_list),
        "voci_attive": voci_tot,
        "materializzato_il": st.get("materializzato_il"),
        "divergenze": div,
        "dettaglio": [
            {"rel": r, "voci": len(voci_attive(r)), "sostituito": _sos(r).exists(),
             "vivo": str(_vivo(r))}
            for r in rel_list
        ],
    }


# ============================================================ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="Corpus vivo dello Studio Coccolo")
    ap.add_argument("--leggi", metavar="REL")
    ap.add_argument("--aggiungi", metavar="REL")
    ap.add_argument("--sostituisci", metavar="REL")
    ap.add_argument("--patch-json", metavar="REL",
                    help="fonde campi in un file-dati .json (sidecar della Sentinella)")
    ap.add_argument("--set", metavar="JSON", default="",
                    help='campi da impostare, es. \'{"stato":"da_ricontrollare"}\'')
    ap.add_argument("--lista", default="", help="lista dentro il JSON, es. voci")
    ap.add_argument("--chiave", default="", help="campo identificativo, es. voce_id")
    ap.add_argument("--id", default="", help="valore della chiave, es. cartabia_processo_civile")
    ap.add_argument("--appendi", action="store_true",
                    help="con --patch-json: AGGIUNGE --set come nuovo elemento di --lista")
    ap.add_argument("--da", metavar="FILE", help="sorgente per --sostituisci ('-' = stdin)")
    ap.add_argument("--testo", default="")
    ap.add_argument("--fonte", default="")
    ap.add_argument("--data", default="")
    ap.add_argument("--sezione", default="")
    ap.add_argument("--tag", default="", help="es. 🟡 per le voci tell-only")
    ap.add_argument("--motivo", default="")
    ap.add_argument("--materializza", action="store_true")
    ap.add_argument("--elenco-vivi", action="store_true")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--promuovi", action="store_true")
    ap.add_argument("--versione", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--forza", action="store_true")
    ap.add_argument("--dove", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.dove:
        print(corpus_dir())
        return 0

    if a.leggi:
        sys.stdout.write(risolvi(a.leggi))
        return 0

    if a.aggiungi:
        v = aggiungi(a.aggiungi, a.testo, a.fonte, a.data, a.sezione, a.tag)
        materializza()
        if v.get("duplicata"):
            print(f"GIA' PRESENTE — {a.aggiungi}: voce identica già scritta, non duplicata.")
            return 0
        print(f"SCRITTA — {normalizza_rel(a.aggiungi)} · {v['data']} · "
              f"fonte: {v['fonte'] or '—'}\n  → vivo: {_vivo(normalizza_rel(a.aggiungi))}")
        return 0

    if a.patch_json:
        try:
            campi = json.loads(a.set) if a.set else {}
        except ValueError as e:
            raise SystemExit(f"[corpus] --set non è JSON valido: {e}")
        v = patch_json(a.patch_json, campi, a.lista, a.chiave, a.id, a.fonte, a.data,
                       azione="append" if a.appendi else "set")
        materializza()
        rel = normalizza_rel(a.patch_json)
        if v.get("duplicata"):
            print(f"GIA' PRESENTE — {rel}: patch identica già registrata.")
            return 0
        print(f"PATCH — {rel} · {v['data']}\n  {v['testo']}\n  → vivo: {_vivo(rel)}")
        return 0

    if a.sostituisci:
        if a.da == "-":
            contenuto = sys.stdin.read()
        elif a.da:
            contenuto = Path(a.da).read_text(encoding="utf-8")
        elif a.testo:
            contenuto = a.testo
        else:
            raise SystemExit("[corpus] --sostituisci richiede --da FILE, --da - o --testo")
        r = sostituisci(a.sostituisci, contenuto, a.motivo)
        materializza()
        print(f"{'CREATO' if r['nuovo'] else 'SOSTITUITO'} — {r['rel']} ({r['byte']} byte)\n"
              f"  → vivo: {_vivo(r['rel'])}")
        return 0

    if a.materializza:
        r = materializza(forza=a.forza)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif r["invariato"]:
            print(f"Invariato — {len(r['file'])} file vivi già aggiornati.")
        else:
            print(f"Materializzati {r['aggiornati']} file vivi in {corpus_dir() / 'vivo'}")
        return 0

    if a.elenco_vivi:
        righe = [{"rel": r, "vivo": str(_vivo(r)), "voci": len(voci_attive(r))}
                 for r in elenco_rel()]
        if a.json:
            print(json.dumps(righe, ensure_ascii=False))
        else:
            for x in righe:
                print(f"{x['rel']}\t{x['vivo']}\t{x['voci']}")
        return 0

    if a.promuovi:
        e = promuovi(a.versione or None, a.dry_run)
        if a.json:
            print(json.dumps(e, ensure_ascii=False, indent=2))
            return 0
        titolo = "SIMULAZIONE — nulla è stato scritto" if a.dry_run else f"PROMOSSO in v{e['versione']}"
        print(f"{titolo}: {len(e['file'])} file · {e['voci']} voci · "
              f"{e['sostituzioni']} sostituzioni")
        for f in e["file"]:
            print(f"  {f['rel']:<50} voci:{f['voci']:<4} "
                  f"{'[sostituzione]' if f['sostituzione'] else ''}")
        if e["saltati"]:
            print(f"  ({len(e['saltati'])} file restano solo nel corpus per scelta — "
                  "archivio locale, escluso dal bundle: " + ", ".join(e["saltati"][:3])
                  + (" …" if len(e["saltati"]) > 3 else "") + ")")
        for d in e["divergenze"]:
            print(f"  ⚠️ DIVERGENZA su {d['rel']} — la base del bundle è cambiata dopo la "
                  f"sostituzione ({d['scritto_il']}): rileggere prima di rilasciare.")
        if not a.dry_run:
            print("\nOra: ricontrolla il diff del repo, poi `python3 scripts/crea_bundle.py`.")
        return 0

    r = stato_report()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"Corpus vivo: {r['corpus']}  (plugin v{r['versione_plugin']})")
    if not r["file_vivi"]:
        print("Nessuna voce locale: il corpus coincide con quello del bundle.")
        return 0
    print(f"{r['file_vivi']} file con contenuto locale · {r['voci_attive']} voci attive · "
          f"materializzato: {r['materializzato_il'] or 'mai'}")
    for d in r["dettaglio"]:
        marca = " [sostituito]" if d["sostituito"] else ""
        print(f"  {d['rel']:<50} {d['voci']} voci{marca}")
    for d in r["divergenze"]:
        print(f"  ⚠️ DIVERGENZA su {d['rel']}: base del bundle cambiata dopo la sostituzione.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
