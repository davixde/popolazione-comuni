#!/usr/bin/env python3
import argparse
import json
import logging

import popolazione_maproulette as pm

DEFAULT_SOURCE = "ANPR 2026"
STATUS_FIXED = "Fixed"
NON_FIXED_STATUSES = ("Created", "Too_Hard", None, "")


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--challenge", required=True,
        help="Esportazione .geojson della challenge MapRoulette")
    parser.add_argument(
        "--geojson", default="comuni-popolazione.geojson",
        help="GeoJSON della challenge da modificare")
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE,
        help="Valore da impostare in source:population")
    parser.add_argument("--overpass-url", default=pm.DEFAULT_OVERPASS_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def rel_id(key):
    if not key:
        return None
    parts = str(key).split("/")
    return parts[-1] if parts else None


def load_challenge(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = {}
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        key = props.get("id")
        rid = rel_id(key)
        if not rid:
            logging.warning("Feature senza 'id' valido, saltata")
            continue
        status = props.get("mr_taskStatus")
        is_fixed = (status == STATUS_FIXED)
        geom = feat.get("geometry") or {}
        out[rid] = {
            "key": key,
            "status": "fixed" if is_fixed else "open",
            "fixed": is_fixed,
            "properties": props,
            "geometry": geom,
        }
    return out


def load_geojson_tasks(path):
    tasks = []
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    for chunk in raw.split("\x1e"):
        line = chunk.strip()
        if not line:
            continue
        fc = json.loads(line)
        feat = fc["features"][0]
        props = feat.get("properties") or {}
        rid = rel_id(props.get("id"))
        tasks.append({"id": rid, "raw": line, "fc": fc})
    return tasks


def set_tags_source(fc, source):
    changed = False
    coop = fc.get("cooperativeWork")
    if not coop:
        return False
    for op_group in coop.get("operations", []):
        data = op_group.get("data")
        for op in (data or {}).get("operations", []):
            if op.get("operation") == "setTags":
                tagset = op.setdefault("data", {})
                if "source:population" not in tagset:
                    tagset["source:population"] = source
                    changed = True
    return changed


def build_source_task(rid, info, source, current_source):
    coords = (info.get("geometry") or {}).get("coordinates")
    if not coords:
        return None
    p = info["properties"]
    osm_id = info["key"]
    task_id = osm_id + "-source"
    new_props = {
        "id": task_id,
        "nome": p.get("nome") or "",
        "nome_csv": p.get("nome_csv") or "",
        "ref_istat": p.get("ref_istat") or "",
        "source_attuale": current_source,
        "source_anpr": source,
        "popolazione_anpr": p.get("popolazione_anpr") or "",
        "data_anpr": p.get("data_anpr") or "",
        "fonte": p.get("fonte") or pm.CSV_URL,
        "url": p.get("url") or "",
        "riga_csv": p.get("riga_csv") or "",
    }
    feature = {
        "type": "Feature",
        "properties": new_props,
        "geometry": {"type": "Point", "coordinates": coords},
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
                        {"operation": "setTags",
                         "data": {"source:population": source}},
                    ],
                },
            }],
        },
    }


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    logging.info("Leggo l'esportazione della challenge: %s", args.challenge)
    challenge = load_challenge(args.challenge)
    logging.info("Task nella challenge: %d", len(challenge))

    fixed = {r for r, i in challenge.items() if i["fixed"]}
    open_tasks = {r for r, i in challenge.items() if not i["fixed"]}
    logging.info("  fixate: %d | non fixate: %d", len(fixed), len(open_tasks))

    logging.info("Query Overpass per source:population ...")
    elements = pm.fetch_overpass(pm.OVERPASS_QUERY, args.overpass_url,
                                 pm.DEFAULT_USER_AGENT, 120.0)
    sources = {}
    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("boundary") != "administrative" or \
           tags.get("admin_level") != "8":
            continue
        sources[str(el.get("id"))] = (tags.get("source:population") or "").strip()
    logging.info("Relazioni Overpass: %d", len(elements))

    tasks = load_geojson_tasks(args.geojson)
    by_id = {t["id"]: t for t in tasks}

    new_tasks = []
    stats = {"new": 0, "absent": 0, "already": 0, "no_geom": 0, "notfound": 0}
    for rid in sorted(fixed):
        info = challenge[rid]
        current_source = sources.get(rid, None)
        if current_source in (None, ""):
            stats["absent"] += 1
            continue
        if current_source == args.source:
            stats["already"] += 1
            continue
        task_id = info["key"] + "-source"
        if task_id in by_id:
            stats["already"] += 1
            continue
        fc = build_source_task(rid, info, args.source, current_source)
        if fc is None:
            stats["no_geom"] += 1
            continue
        new_tasks.append((rid, fc, current_source))
        stats["new"] += 1
    modified = 0
    modified_ids = []
    missing_open = []
    for rid in sorted(open_tasks):
        t = by_id.get(rid)
        if t is None:
            missing_open.append(rid)
            continue
        if set_tags_source(t["fc"], args.source):
            modified += 1
            modified_ids.append(rid)

    logging.info("=== RIEPILOGO (%s dry-run) ===",
                 "SOLO ANALISI" if args.dry_run else "SCRITTURA")
    logging.info("Task challenge totali: %d", len(challenge))
    logging.info("  fixate:             %d", len(fixed))
    logging.info("  non fixate:         %d", len(open_tasks))
    logging.info(
        "Nuove task source per comuni fixati con source diversa da '%s': %d",
        args.source, stats["new"])
    logging.info("  di cui source assente su OSM (ESCLUSE): %d",
                 stats["absent"])
    logging.info("  di cui source gia' pari a '%s' (ESCLUSE): %d",
                 args.source, stats["already"])
    logging.info("  prive di geometria / non trovate su OSM: %d",
                 stats["no_geom"] + stats["notfound"])
    logging.info("Task non fixate a cui verra' aggiunto source:population: %d",
                 modified)
    if missing_open:
        logging.warning("Task non fixate NON presenti nel geojson (%d): %s",
                        len(missing_open), missing_open[:10])

    if args.dry_run:
        for rid, fc, cur in list(new_tasks[:3]):
            logging.info("  [ESEMPIO nuova] %s source '%s' -> '%s'",
                         rid, cur, args.source)
        if modified_ids:
            rid = modified_ids[0]
            t = by_id[rid]
            feat = t["fc"]["features"][0]
            ops = (t["fc"].get("cooperativeWork", {})
                   .get("operations", [])[0].get("data", {})
                   .get("operations", []))
            logging.info("  [ESEMPIO modificata] %s operations: %s",
                         feat["properties"].get("id"),
                         json.dumps(ops, ensure_ascii=False))
        logging.info("DRY-RUN: nessun file scritto.")
        return 0

    logging.info("Scrivo %s ...", args.geojson)
    with open(args.geojson, "w", encoding="utf-8") as out:
        written = 0
        for t in tasks:
            out.write("\x1e" + json.dumps(t["fc"], ensure_ascii=False) + "\n")
            written += 1
        for rid, fc, _cur in new_tasks:
            out.write("\x1e" + json.dumps(fc, ensure_ascii=False) + "\n")
            written += 1

    logging.info("Fatto: %d righe scritte (di cui %d nuove task source).",
                 written, len(new_tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())