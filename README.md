# Studio Coccolo — corpus normativo

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
