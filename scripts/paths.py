"""Helper di percorsi PORTABILE (Mac + Windows). Niente path assoluti cablati."""
import sys
if sys.platform == "win32":  # Cowork/Desktop su Windows: le pipe sono cp1252 → UTF-8 (accenti, frecce, emoji)
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
from pathlib import Path
import os

def project_root() -> Path:
    return Path(__file__).resolve().parents[1]

def claude_skills_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "skills"
    return Path.home() / "Library" / "Application Support" / "Claude" / "skills"

ROOT = project_root()
WIKI = ROOT / "wiki-studio"
AUTORI = WIKI / "autori"


# --------------------------------------------------------------------- stato persistente

def _scrivibile(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".probe-scrittura"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


#: cartella dello stato di SESSIONE dentro la cartella dell'attivita'
SESSIONE = ".studio-sessione"
_RX_VM = None


def _base_da_cwd(cwd: str) -> Path:
    """In Cowork la VM monta la cartella dell'attivita' in /sessions/<x>/mnt/outputs: qualunque cwd sotto
    /sessions/<x>/mnt/ (anche una cartella del fascicolo) riporta li'."""
    global _RX_VM
    import re
    if _RX_VM is None:
        _RX_VM = re.compile(r"^(/sessions/[^/]+/mnt)(?:/|$)")
    m = _RX_VM.match(str(cwd or ""))
    if m and (Path(m.group(1)) / "outputs").is_dir():
        return Path(m.group(1)) / "outputs"
    return Path(cwd)


def base_sessione() -> Path:
    """La cartella dell'ATTIVITA' in cui vive lo stato della pratica (v0.30).

    In Cowork gli hook girano sul computer e gli script nella VM: l'unica cartella che vedono entrambi e'
    quella dell'attivita' (`outputs`). Ordine:
      1. $STUDIO_SESSIONE_BASE — la imposta l'hook con il `cwd` del proprio stdin (imposta_base);
      2. nella VM di Cowork: /sessions/<x>/mnt/outputs, qualunque sia il cwd (il modello puo' aver fatto cd);
      3. risalendo dal cwd, la prima cartella che contiene gia' `.studio-sessione/identita.json`;
      4. il cwd.
    """
    env = os.environ.get("STUDIO_SESSIONE_BASE")
    if env:
        return Path(env)
    cwd = os.getcwd()
    b = _base_da_cwd(cwd)
    if b != Path(cwd):
        return b
    try:
        vm = [p for p in Path("/sessions").glob("*/mnt/outputs") if p.is_dir()] if Path("/sessions").is_dir() else []
    except OSError:
        vm = []
    if len(vm) == 1:
        return vm[0]
    p = Path(cwd)
    for anc in [p] + list(p.parents)[:8]:
        if (anc / SESSIONE / "identita.json").exists():
            return anc
    return p


def imposta_base(cwd) -> None:
    """Per gli hook: la cartella dell'attivita' e' il `cwd` dello stdin. Vale anche per i processi figli."""
    if cwd and not os.environ.get("STUDIO_SESSIONE_BASE"):
        os.environ["STUDIO_SESSIONE_BASE"] = str(_base_da_cwd(str(cwd)))


def is_cowork() -> bool:
    b = str(base_sessione())
    return b.startswith("/sessions/") or "local-agent-mode-sessions" in b


def stato_root() -> Path:
    """Radice dello stato di SESSIONE (v0.30): `<cartella dell'attivita'>/.studio-sessione`.

    Pratica corrente, chiave delle ricevute, autorizzazioni, telemetria, ledger, cache delle fonti, corpus
    sincronizzato: tutto cio' che hook e script devono condividere, e che deve morire con l'attivita'
    (decisione di Edoardo, 25/09/2026: una pratica si apre e si chiude nella stessa attivita' Cowork).
    $STUDIO_STATO_ROOT resta l'override esplicito (test)."""
    env = os.environ.get("STUDIO_STATO_ROOT")
    if env:
        return Path(env)
    return base_sessione() / SESSIONE


def stato_root_persistente() -> Path:
    """La conoscenza che deve sopravvivere alle sessioni (corpus vivo di Edoardo, stato della sentinella dei
    modelli, calibrazione degli esiti): la vecchia cascata. $STUDIO_STATO_PERSISTENTE per i test."""
    env = os.environ.get("STUDIO_STATO_PERSISTENTE") or os.environ.get("STUDIO_STATO_ROOT")
    if env:
        return Path(env)
    fratello = ROOT.parent / ".studio-coccolo"
    if fratello.exists() or _scrivibile(fratello):
        return fratello
    return Path.home() / ".studio-coccolo"


def rel_base(p) -> str:
    """Percorso RELATIVO alla cartella dell'attivita' se ci sta dentro (valido dall'host e dalla VM), altrimenti
    assoluto. Anche un percorso VM /sessions/<x>/mnt/outputs/... diventa relativo."""
    import re
    s = str(p)
    m = re.match(r"^/sessions/[^/]+/mnt/outputs/(.+)$", s)
    if m:
        return m.group(1)
    try:
        return Path(s).resolve().relative_to(base_sessione().resolve()).as_posix()
    except (ValueError, OSError):
        return s


def da_base(s) -> Path:
    """Inverso di rel_base: relativo → sotto la cartella dell'attivita'; percorso VM → mappato sulla base locale."""
    import re
    s = str(s or "")
    m = re.match(r"^/sessions/[^/]+/mnt/outputs/(.+)$", s)
    if m:
        return base_sessione() / m.group(1)
    p = Path(s)
    return p if p.is_absolute() else base_sessione() / p


if __name__ == "__main__":
    print("project_root:", project_root())
    print("skills dir  :", claude_skills_dir())
    print("wiki        :", WIKI)
    print("base attivita:", base_sessione())
    print("stato_root  :", stato_root(), "(sessione)")
    print("persistente :", stato_root_persistente())
