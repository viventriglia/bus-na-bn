# BN NA Bus

Interfaccia statica per consultare le corse presenti in `orari_eav_air.csv`.

La pagina usa `data.js` per funzionare anche quando `index.html` viene aperto direttamente con `file://`.
Il CSV resta il file sorgente da modificare.

## Pubblicazione su GitHub Pages

1. Carica questi file nel repository:
   - `index.html`
   - `styles.css`
   - `app.js`
   - `data.js`
   - `orari_eav_air.csv`
2. In GitHub vai su `Settings > Pages`.
3. Seleziona il branch da pubblicare e la cartella root.

Non serve una build web: la pagina filtra i dati nel browser.

## Aggiornare gli orari

Sostituisci `orari_eav_air.csv` mantenendo le stesse colonne:

```csv
orario partenza;orario arrivo;stazione partenza;stazione arrivo;linea
```

Poi rigenera `data.js`:

```bash
python scripts/generate_data.py
```
