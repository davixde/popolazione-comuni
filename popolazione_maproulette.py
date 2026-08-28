#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Il programma:

1. legge il CSV popolazione_residente_export.csv di anpr-opendata;
2. interroga Overpass per le relazioni comunali
   boundary=administrative + admin_level=8 con il tag
   ref:ISTAT presenti in Italia;
3. associa ogni relazione al comune del CSV tramite il codice ISTAT
   (ref:ISTAT <-> COD_ISTAT_COMUNE, normalizzato a 6 cifre);
4. per ogni comune in cui il tag population differisce dal valore
   ANPR (o manca) crea una task che imposta population e
   date:population; se la popolazione e' gia' corretta ma manca
   date:population la task imposta solo la data;
"""

import argparse
import csv
import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

CSV_URL = ("https://raw.githubusercontent.com/italia/anpr-opendata/main/"
           "data/popolazione_residente_export.csv")

# Pagina GitHub del CSV (per il link alla riga del singolo comune).
CSV_GITHUB_URL = ("https://github.com/italia/anpr-opendata/blob/main/"
                  "data/popolazione_residente_export.csv")

OVERPASS_QUERY = """[out:json][timeout:300];
area["ISO3166-1"="IT"]["admin_level"="2"]->.it;
(
  relation["boundary"="administrative"]["admin_level"="8"]["ref:ISTAT"](area.it);
);
out center;
"""

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_USER_AGENT = "popolazione-comuni-maproulette/1.0 (contatto@example.org)"


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=CSV_URL)
    parser.add_argument("--output", default="comuni-popolazione.geojson")
    parser.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL,
                        help="Endpoint Overpass.")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Timeout HTTP in secondi.")
    parser.add_argument("--verbose", action="store_true",
                        help="Log piu' dettagliato.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_text(url, user_agent, timeout, retries=3):
    """GET di una URL e restituisce il testo (per il CSV)."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
    }
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            if exc.code in (406, 429, 403, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 3 * (2 ** attempt)
                logging.warning("HTTP %s: nuovo tentativo tra %.0fs (%s)",
                                exc.code, wait, url)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def http_get(url, user_agent, timeout, retries=3, accept=None):
    """GET JSON di una URL, con retry su errori temporanei (406/429/5xx)."""
    headers = {"User-Agent": user_agent}
    if accept:
        headers["Accept"] = accept
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (406, 429, 403, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 3 * (2 ** attempt)
                logging.warning("HTTP %s: nuovo tentativo tra %.0fs (%s)",
                                exc.code, wait, url)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


# ---------------------------------------------------------------------------
# Normalizzazione
# ---------------------------------------------------------------------------


def normalize_istat(code):
    """Normalizza un codice ISTAT a 6 cifre (con zeri iniziali).

    Gestisce sia il formato del CSV (069001) sia eventuali valori OSM
    senza zeri iniziali (69001) o con spazi.
    """
    if code is None:
        return None
    digits = re.sub(r"\D", "", str(code).strip())
    if not digits:
        return None
    return digits.zfill(6)


def iso_date(value):
    """Converte una data GG-MM-AAAA (formato CSV ANPR) in AAAA-MM-GG.

    Restituisce None se il formato non e' riconosciuto.
    """
    value = (value or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def norm_population(value):
    """Normalizza un tag population per il confronto.

    Ignora separatori di migliaia (49.177, 49 177) e restituisce un
    intero, oppure None se il valore non contiene cifre.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dati ANPR
# ---------------------------------------------------------------------------


def load_population(source, user_agent, timeout):
    """Legge il CSV ANPR da un percorso locale o da una URL.

    Restituisce un dict {codice_istat: {...}} con nome del comune,
    popolazione residente e data di elaborazione in formato ISO.
    """
    if source.startswith(("http://", "https://")):
        logging.info("Download del CSV ANPR da %s", source)
        text = http_get_text(source, user_agent, timeout)
    else:
        with open(source, "r", encoding="utf-8-sig") as fh:
            text = fh.read()

    comuni = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        istat = normalize_istat(row.get("COD_ISTAT_COMUNE"))
        if not istat:
            continue
        nome = (row.get("COMUNE") or "").strip()
        pop_raw = (row.get("RESIDENTI") or "").strip()
        data_raw = (row.get("DATA_ELABORAZIONE") or "").strip()
        if not pop_raw.isdigit():
            logging.warning("Riga senza RESIDENTI valido: %s (%s)", istat, nome)
            continue
        data = iso_date(data_raw)
        if not data:
            logging.warning("DATA_ELABORAZIONE non valida: %s (%s): %r",
                            istat, nome, data_raw)
            continue
        comuni[istat] = {
            "nome": nome,
            "popolazione": int(pop_raw),
            "data": data,
            "riga_csv": reader.line_num,
        }
    return comuni


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------


def fetch_overpass(query, url, user_agent, timeout):
    """Esegue la query Overpass e restituisce gli elementi."""
    target = "{}?data={}".format(url, urllib.parse.quote(query))
    data = http_get(target, user_agent, timeout, accept="*/*")
    return data.get("elements", [])


# ---------------------------------------------------------------------------
# Costruzione della task
# ---------------------------------------------------------------------------


def build_task(element, comune, pop_attuale, data_attuale):
    """Costruisce il FeatureCollection per una relazione, o None."""
    osm_id = "relation/%d" % element["id"]
    center = element.get("center") or {}
    lon, lat = center.get("lon"), center.get("lat")
    if lon is None or lat is None:
        return None
    tags = element.get("tags") or {}

    if pop_attuale == comune["popolazione"]:
        # Popolazione gia' corretta: manca solo la data.
        new_tags = {"population:date": comune["data"]}
    else:
        new_tags = {
            "population": str(comune["popolazione"]),
            "population:date": comune["data"],
        }

    feature = {
        "type": "Feature",
        "properties": {
            "id": osm_id,
            # nome attuale su OSM (vuoto se assente) e nome ANPR dal CSV:
            # servono al review per confrontare i due.
            "nome": tags.get("name") or "",
            "nome_csv": comune["nome"],
            "ref_istat": comune["ref_istat"],
            "popolazione_attuale": (str(pop_attuale)
                                    if pop_attuale is not None else ""),
            "popolazione_anpr": str(comune["popolazione"]),
            "data_attuale": data_attuale,
            "data_anpr": comune["data"],
            "fonte": CSV_URL,
            "url": "{}#L{}".format(CSV_GITHUB_URL, comune["riga_csv"]),
            # solo il numero di riga: il "#L" va messo nella parte
            # statica del link, perche' MapRoulette URL-encoda i valori
            # dei mustache dentro gli hyperlink (e %23 darebbe 404).
            "riga_csv": str(comune["riga_csv"]),
        },
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }
    return {
        "type": "FeatureCollection",
        "features": [feature],
        "cooperativeWork": {
            "meta": {"version": 2, "type": 1},
            "operations": [{
                "operationType": "modifyElement",
                "data": {
                    "id": osm_id,
                    "operations": [
                        {"operation": "setTags", "data": new_tags},
                    ],
                },
            }],
        },
    }


# ---------------------------------------------------------------------------
# Flusso principale
# ---------------------------------------------------------------------------


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    comuni = load_population(args.csv, DEFAULT_USER_AGENT, args.timeout)
    logging.info("Comuni dal CSV ANPR: %d", len(comuni))

    logging.info("Query Overpass in corso...")
    elements = fetch_overpass(OVERPASS_QUERY, args.overpass_url,
                              DEFAULT_USER_AGENT, args.timeout)
    logging.info("Relazioni OSM con ref:ISTAT: %d", len(elements))

    written = 0
    skipped = 0
    no_csv = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for el in elements:
            if el.get("type") != "relation":
                continue
            tags = el.get("tags") or {}
            istat = normalize_istat(tags.get("ref:ISTAT"))
            if not istat:
                skipped += 1
                continue
            comune = comuni.get(istat)
            if comune is None:
                no_csv += 1
                logging.debug("Comune %s non presente nel CSV, saltato", istat)
                continue
            comune = dict(comune)
            comune["ref_istat"] = istat

            pop_attuale = norm_population(tags.get("population"))
            data_attuale = (tags.get("date:population") or "").strip()

            # Regola: task se la population differisce (o manca) oppure se
            # la population e' corretta ma manca date:population. Se entrambi
            # i tag sono gia' allineati non si crea nulla: la data di
            # elaborazione ANPR cambia ogni giorno e aggiornare solo la data
            # genererebbe task inutili.
            if pop_attuale == comune["popolazione"] and data_attuale:
                skipped += 1
                continue

            fc = build_task(el, comune, pop_attuale, data_attuale)
            if fc is None:
                skipped += 1
                continue
            out.write("\x1e" + json.dumps(fc, ensure_ascii=False) + "\n")
            written += 1

    logging.info("Task scritte: %d, saltate: %d, senza riga CSV: %d",
                 written, skipped, no_csv)


if __name__ == "__main__":
    main()
