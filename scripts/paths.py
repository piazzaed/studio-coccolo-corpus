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


def stato_root() -> Path:
    """Radice dello stato persistente (ledger, cache fonti, corpus vivo, testi locali).

    PERCHE' NON ~/.studio-coccolo E BASTA (v0.16, misurato su Cowork)
    -----------------------------------------------------------------
    Due profilazioni indipendenti (11/08 e 22/08/2026) hanno trovato il ledger VUOTO a fine
    pratica nonostante i --put: nell'ambiente Cowork Desktop la $HOME e' effimera — viene
    azzerata tra una sessione e l'altra — mentre la cartella di lavoro sincronizzata
    sopravvive. La v0.15 aveva spostato lo stato in $HOME proprio per farlo sopravvivere,
    e su Cowork ha ottenuto l'effetto opposto: la vecchia cache RELATIVA alla CWD era
    sopravvissuta a tre sessioni, quella "persistente" in $HOME moriva con ognuna.

    La scelta ora e' una CASCATA, dalla piu' longeva alla meno:
      1. $STUDIO_STATO_ROOT              (test e override espliciti)
      2. <accanto al plugin>/.studio-coccolo   — fratello della cartella del plugin: vive
         nell'area sincronizzata (sopravvive alle sessioni Cowork) e FUORI dal pacchetto
         (sopravvive alla reinstallazione del bundle). Se il genitore non e' scrivibile
         (plugin montato in un punto read-only), si passa oltre.
      3. ~/.studio-coccolo               — il comportamento v0.15 (Mac/Windows normali).

    Le vecchie posizioni restano LEGGIBILI dai singoli consumatori (ledger, cache): la
    migrazione e' read-through, niente va perso.
    """
    env = os.environ.get("STUDIO_STATO_ROOT")
    if env:
        return Path(env)
    fratello = ROOT.parent / ".studio-coccolo"
    if fratello.exists() or _scrivibile(fratello):
        return fratello
    return Path.home() / ".studio-coccolo"


#: Posizioni storiche dello stato, in ordine di probabilita': i consumatori le consultano in
#: lettura quando un dato non si trova nella stato_root() corrente.
def stato_root_legacy() -> list:
    out = []
    home = Path.home() / ".studio-coccolo"
    if home != stato_root():
        out.append(home)
    return out


if __name__ == "__main__":
    print("project_root:", project_root())
    print("skills dir  :", claude_skills_dir())
    print("wiki        :", WIKI)
    print("stato_root  :", stato_root())
    print("legacy      :", [str(p) for p in stato_root_legacy()])
