#!/usr/bin/env python3
"""
build.py: builds every backdrop listed in backdrops.txt and rewrites the
README with their links. GitHub Actions runs this every day.

  python generator/build.py --work work --site . [--only everything|services|bingecat]

--work  folder holding the prism-wallpapers script at scripts/backdrop_T2_flat.py
        (used for its image downloading and flat T2 layout/rendering)
--site  this repo's folder (images + README are written here)

How titles are picked
  Streaming services: what's trending on TMDB this week AND streaming on that
    service (UK first; services not in the UK, like Hulu, use the US).
    Trending titles go in the sharp, in-focus area; the rest of the grid is
    filled with the most popular titles currently on that service.
  Studios: what's trending this week from that studio, filled the same way.
  Bingecat: your Bingecat AI lists ("Latest" lists in focus).

If a backdrop fails, its previous image stays in place, so links never break.
"""
import argparse
import csv
import importlib.util
import io
import math
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import requests

SECTIONS = {  # type -> (folder, README heading)
    "network": ("networks", "Streaming services"),
    "company": ("companies", "Studios"),
    "genre": ("genres", "Genres"),
    "bingecat": ("bingecat", "Recommended for you"),
}
IMAGE = "t2_flat_4k.jpg"
TITLES_PER_BACKDROP = 60
TRENDING_PAGES = 10          # pages of TMDB trending per type (20 titles a page)
TRENDING_WINDOW = "week"     # "week" = steadier ranking, "day" = more reactive
NETFLIX_TOP10_URL = os.environ.get("NETFLIX_TOP10_URL", "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv")
NETFLIX_COUNTRY = "GB"       # Netflix's official weekly Top 10 for this country
FILLER_PAGES = 3             # pages of "popular on this service" per type
REGIONS = ["GB", "US"]       # tried in this order

# TMDB network ID (as used in backdrops.txt) -> the streaming service on TMDB:
# its exact provider IDs first, then exact names as a fallback (never partial names,
# so e.g. "Apple TV" can't sweep in other channels sold through the Apple TV app).
_HBO = {"ids": [1899, 384], "names": ["HBO Max", "Max"]}
_PARAMOUNT = {"ids": [531, 2303], "names": ["Paramount Plus", "Paramount Plus Premium", "Paramount+"]}
SERVICES = {
    "213": {"ids": [8, 1796], "names": ["Netflix", "Netflix basic with Ads"]},
    "49": _HBO,
    "8304": _HBO,
    "2739": {"ids": [337], "names": ["Disney Plus", "Disney+"]},
    "2552": {"ids": [350], "names": ["Apple TV Plus", "Apple TV+"]},
    "1024": {"ids": [9, 119, 2100], "names": ["Amazon Prime Video", "Amazon Prime Video with Ads"]},
    "453": {"ids": [15], "names": ["Hulu"]},
    "4330": _PARAMOUNT,
    "6100": _PARAMOUNT,
    "3353": {"ids": [386, 387], "names": ["Peacock Premium", "Peacock Premium Plus", "Peacock"]},
    "1112": {"ids": [283], "names": ["Crunchyroll"]},
}

# Genres: TMDB uses different genre IDs for films and TV, and has no TV genre for
# horror, romance or thriller, so each genre maps to its own film + TV IDs.
GENRES = {
    "action":          {"movie": [28],    "tv": [10759]},          # TV: Action & Adventure
    "adventure":       {"movie": [12],    "tv": [10759]},
    "animation":       {"movie": [16],    "tv": [16], "exclude_lang": "ja"},  # anime has its own
    "anime":           {"movie": [16],    "tv": [16], "lang": "ja"},          # Japanese animation
    "comedy":          {"movie": [35],    "tv": [35]},
    "crime":           {"movie": [80],    "tv": [80]},
    "documentary":     {"movie": [99],    "tv": [99]},
    "drama":           {"movie": [18],    "tv": [18]},
    "family":          {"movie": [10751], "tv": [10751]},
    "fantasy":         {"movie": [14],    "tv": [10765]},          # TV: Sci-Fi & Fantasy
    "horror":          {"movie": [27],    "tv": []},               # no TV horror genre on TMDB
    "reality-tv":      {"movie": [],      "tv": [10764]},
    "romance":         {"movie": [10749], "tv": []},               # no TV romance genre on TMDB
    "science-fiction": {"movie": [878],   "tv": [10765]},
    "thriller":        {"movie": [53],    "tv": [9648]},           # TV: Mystery (closest match)
    "war":             {"movie": [10752], "tv": [10768]},          # TV: War & Politics
    "western":         {"movie": [37],    "tv": [37]},
}

# What counts as a service's own "originals" (go first in the sharp area):
# shows made by that network, or films/shows from these studios (matched on name).
ORIGINALS = {
    "213": ["netflix"],
    "49": ["hbo", "warner bros"],
    "8304": ["hbo", "warner bros"],
    "2739": ["disney", "pixar", "marvel", "lucasfilm"],
    "2552": ["apple"],
    "1024": ["amazon", "prime video"],
    "453": ["hulu", "fx"],
    "4330": ["paramount"],
    "6100": ["paramount"],
    "3353": ["peacock", "universal"],
    "1112": ["crunchyroll"],
}

TMDB_BASE = os.environ.get("TMDB_API_BASE", "https://api.themoviedb.org/3")
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
_print_lock = threading.Lock()


def log(msg=""):
    with _print_lock:
        print(msg, flush=True)


# ── Config ───────────────────────────────────────────────────────────

def parse_config(path):
    """Lines look like:  network 49 8304 = HBO + HBO Max   [| region: US] [| watch: HBO Max]"""
    entries = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            sys.exit(f"✗ backdrops.txt line {n}: missing '= Name'  →  {raw.strip()}")
        left, right = line.split("=", 1)
        name, *opt_parts = [p.strip() for p in right.split("|")]
        opts = {}
        for p in opt_parts:
            if ":" in p:
                k, v = p.split(":", 1)
                opts[k.strip().lower()] = v.strip()
        parts = left.split()
        if len(parts) < 2 or parts[0] not in SECTIONS or not name:
            sys.exit(f"✗ backdrops.txt line {n}: expected '<network|company|bingecat> <id> = Name'  →  {raw.strip()}")
        kind, ids = parts[0], parts[1:]
        if kind == "bingecat":
            if ids[0] not in ("films", "tv"):
                sys.exit(f"✗ backdrops.txt line {n}: bingecat needs 'films' or 'tv'")
            if "profile" in opts and not re.fullmatch(r"\d+", opts["profile"]):
                sys.exit(f"✗ backdrops.txt line {n}: profile should be the Bingecat profile number, e.g. 7047")
        elif kind == "genre":
            key = ids[0].lower()
            if key not in GENRES and not ("movie" in opts or "tv" in opts):
                sys.exit(f"✗ backdrops.txt line {n}: unknown genre '{ids[0]}'. Known: {', '.join(GENRES)}"
                         f" (or add '| movie: <ids> | tv: <ids>')")
            ids = [key]
        elif not all(re.fullmatch(r"\d+(-movies|-tv)?", i) for i in ids):
            sys.exit(f"✗ backdrops.txt line {n}: IDs must be numbers  →  {raw.strip()}")
        entries.append({"type": kind, "ids": ids, "name": name, "opts": opts})
    return entries


# ── Small helpers ────────────────────────────────────────────────────

def folder_matches(folder_name, first_id):
    """'213-netflix' matches 213, but '213-movies-netflix' only matches 213-movies."""
    if not folder_name.startswith(first_id + "-"):
        return False
    if re.fullmatch(r"\d+", first_id):
        rest = folder_name[len(first_id) + 1:]
        return not (rest.startswith("movies-") or rest.startswith("tv-"))
    return True


def find_folder(parent, first_id):
    if not parent.is_dir():
        return None
    hits = sorted(p for p in parent.iterdir() if p.is_dir() and folder_matches(p.name, first_id))
    return hits[0] if hits else None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "backdrop"


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("+", "plus"))


def name_matches(provider_name, wanted):
    return norm(provider_name) in {norm(w) for w in wanted}


def split_ids(ids):
    """['213-movies'] -> (['213'], 'movie');  ['49','8304'] -> (['49','8304'], None)"""
    only, clean = None, []
    for i in ids:
        if i.endswith("-movies"):
            only = "movie"
        elif i.endswith("-tv"):
            only = "tv"
        clean.append(i.split("-")[0])
    return clean, only


# ── TMDB ─────────────────────────────────────────────────────────────

def tmdb(endpoint, params=None):
    p = {"api_key": TMDB_KEY, "language": "en-US", "include_adult": "false", **(params or {})}
    for attempt in range(4):
        try:
            r = requests.get(f"{TMDB_BASE}{endpoint}", params=p, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return {}
            return r.json()
        except Exception:
            time.sleep(2 * (attempt + 1))
    return {}


class TMDBData:
    """Everything shared between backdrops: streaming-service directory + today's trending."""

    def __init__(self, mod):
        self.mod = mod
        self._providers = {}
        self._lock = threading.Lock()
        self.trending = []

    def providers(self, region):
        with self._lock:
            return self._providers_unlocked(region)

    def _providers_unlocked(self, region):
        if region not in self._providers:
            found = {}
            for kind in ("movie", "tv"):
                for p in tmdb(f"/watch/providers/{kind}", {"watch_region": region}).get("results", []):
                    found[p["provider_id"]] = p["provider_name"]
            self._providers[region] = found
        return self._providers[region]

    def ok(self, item):
        if not item or not item.get("id") or item.get("adult"):
            return False
        if not (item.get("poster_path") or item.get("backdrop_path")):
            return False
        try:
            return not self.mod._is_adult_content(item)
        except Exception:
            return True

    def load_trending(self):
        ranked = []
        for kind in ("movie", "tv"):
            pos = 0
            for page in range(1, TRENDING_PAGES + 1):
                results = tmdb(f"/trending/{kind}/{TRENDING_WINDOW}", {"page": page}).get("results", [])
                if not results:
                    break
                for item in results:
                    ranked.append((pos, kind, item))
                    pos += 1
        ranked.sort(key=lambda r: (r[0], r[1]))  # interleave films and shows by rank

        def details(row):
            pos, kind, item = row
            d = tmdb(f"/{kind}/{item['id']}", {"append_to_response": "watch/providers"})
            if not self.ok(d):
                return None
            regions = (d.get("watch/providers") or {}).get("results") or {}
            return {
                "kind": kind,
                "item": d,
                "flatrate": {r: {p["provider_id"] for p in (v.get("flatrate") or [])}
                             for r, v in regions.items()},
                "companies": {str(c["id"]) for c in d.get("production_companies") or []},
                "networks": {str(n["id"]) for n in d.get("networks") or []},
                "genres": {g["id"] for g in d.get("genres") or []},
                "lang": d.get("original_language") or "",
                "makers": [(x.get("name") or "").lower() for x in
                           (d.get("networks") or []) + (d.get("production_companies") or [])],
            }

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.trending = [r for r in pool.map(details, ranked) if r]
        log(f"  {len(self.trending)} titles trending today (films + shows)")

    def discover(self, kind, params):
        out = []
        for page in range(1, FILLER_PAGES + 1):
            results = tmdb(f"/discover/{kind}", {"sort_by": "popularity.desc", "page": page, **params}).get("results", [])
            out += [(kind, i) for i in results if self.ok(i)]
            if len(results) < 20:
                break
        return out


# ── Netflix's official weekly Top 10 ─────────────────────────────────

_netflix_cache = {}
_netflix_lock = threading.Lock()


# ── Real per-service Top 10s (FlixPatrol data, via the AIOMetadata addon) ──

AIOMETADATA_SLUGS = {   # TMDB network ID in backdrops.txt -> AIOMetadata "flixpatrol.<slug>.<region>" lists
    "213": "netflix", "49": "hbo-max", "8304": "hbo-max", "2739": "disney", "2552": "apple-tv",
    "1024": "amazon-prime", "453": "hulu", "4330": "paramount", "6100": "paramount",
    "3353": "peacock", "1112": "crunchyroll",
}
_aio_cache = {}
_aio_lock = threading.Lock()


def metas_to_tmdb(metas):
    """Stremio catalog items (IMDb 'tt…' or 'tmdb:…' ids) -> (kind, TMDB item) in the same order."""
    out = []
    for m in metas:
        mid = m.get("id") or ""
        kind = "tv" if m.get("type") in ("series", "tv", "anime") else "movie"
        item = None
        if mid.startswith("tt"):
            found = tmdb(f"/find/{mid}", {"external_source": "imdb_id"})
            hits = found.get(f"{kind}_results") or found.get("tv_results" if kind == "movie" else "movie_results") or []
            if hits:
                item = hits[0]
                kind = "tv" if found.get("tv_results") and item in found["tv_results"] else kind
        elif mid.startswith("tmdb:"):
            item = tmdb(f"/{kind}/{mid.split(':', 1)[1]}") or None
        if item and item.get("id") and not item.get("adult") and \
                (item.get("poster_path") or item.get("backdrop_path")):
            out.append((kind, item))
    return out


def service_top10(network_id):
    """The service's Top 10 shows + films, interleaved (show #1, film #1, show #2 …).
    Returns (titles, label) or ([], None) if AIOMetadata isn't set up / has no list for it."""
    manifest_url = os.environ.get("AIOMETADATA_MANIFEST_URL", "").strip()
    slug = AIOMETADATA_SLUGS.get(network_id)
    if not manifest_url or not slug:
        return [], None
    with _aio_lock:
        if "manifest" not in _aio_cache:
            try:
                base = manifest_url.split("?", 1)[0]
                base = base[: -len("/manifest.json")] if base.endswith("/manifest.json") else base
                _aio_cache["manifest"] = (base, get_json(base + "/manifest.json").get("catalogs", []))
            except Exception as exc:
                log(f"  ! AIOMetadata unavailable ({exc}); using TMDB trending instead")
                _aio_cache["manifest"] = (None, [])
        base, catalogs = _aio_cache["manifest"]
    if not base:
        return [], None
    mine = [c for c in catalogs if c.get("id", "").startswith(f"flixpatrol.{slug}.")]
    if not mine:
        return [], None
    mine.sort(key=lambda c: {"series": 0, "all": 0}.get(c.get("type"), 1))   # shows first, then films
    lists = []
    for c in mine:
        try:
            url = catalog_url(base, c, 0)
            lists.append(get_json(url).get("metas", [])[:10] if url else [])
        except Exception as exc:
            log(f"  ! couldn't read {c['id']} ({exc})")
    longest = max((len(x) for x in lists), default=0)
    ordered = [lst[i] for i in range(longest) for lst in lists if i < len(lst)]
    titles = metas_to_tmdb(ordered)
    region = mine[0]["id"].split(".")[2].upper() if mine[0]["id"].count(".") >= 3 else "?"
    return titles, f"real {region}"


def netflix_official(data):
    """Netflix's own Top 10 for NETFLIX_COUNTRY, latest week, as TMDB titles:
    TV #1, Film #1, TV #2, Film #2 ...  Returns (titles, week) or ([], None) if unavailable."""
    with _netflix_lock:
        if "result" in _netflix_cache:
            return _netflix_cache["result"]
        result = ([], None)
        try:
            r = requests.get(NETFLIX_TOP10_URL, timeout=60, headers={"User-Agent": "prism-backdrops"})
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text), delimiter="\t"))
            rows = [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k} for row in rows]
            mine = [x for x in rows if x.get("country_iso2", "").upper() == NETFLIX_COUNTRY]
            week = max(x.get("week", "") for x in mine)
            latest = [x for x in mine if x.get("week") == week]
            tv = sorted([x for x in latest if "tv" in x.get("category", "").lower()],
                        key=lambda x: int(x.get("weekly_rank") or 99))
            films = sorted([x for x in latest if "film" in x.get("category", "").lower()],
                           key=lambda x: int(x.get("weekly_rank") or 99))
            order = [x for pair in zip(tv, films) for x in pair] + tv[len(films):] + films[len(tv):]
            titles = []
            for x in order:
                kind = "tv" if "tv" in x.get("category", "").lower() else "movie"
                name = x.get("show_title") or x.get("season_title", "").split(":")[0]
                hits = tmdb(f"/search/{kind}", {"query": name}).get("results", [])
                key = "name" if kind == "tv" else "title"
                exact = [h for h in hits if (h.get(key) or "").lower() == name.lower()]
                pick = next((h for h in exact + hits if data.ok(h)), None)
                if pick:
                    titles.append((kind, pick))
            result = (titles, week)
            log(f"  Netflix official {NETFLIX_COUNTRY} Top 10 (week of {week}): {len(titles)} titles")
        except Exception as exc:
            log(f"  ! Netflix official Top 10 unavailable ({exc}), using TMDB trending for Netflix")
        _netflix_cache["result"] = result
        return result


# ── Picking titles ───────────────────────────────────────────────────

def resolve_service(data, entry):
    """Which streaming service (region, provider IDs, names) a network entry means, or None."""
    clean, _ = split_ids(entry["ids"])
    if "watch" in entry["opts"]:
        known = {"ids": [], "names": [w.strip() for w in entry["opts"]["watch"].split(",")]}
    else:
        known = SERVICES.get(clean[0])
    if not known:
        return None
    regions = [entry["opts"]["region"].upper()] if "region" in entry["opts"] else REGIONS
    for region in regions:
        available = data.providers(region)
        ids = {i for i in known["ids"] if i in available}
        if not ids:
            ids = {pid for pid, pname in available.items() if name_matches(pname, known["names"])}
        if ids:
            return region, ids, sorted({available[i] for i in ids})
    return None


def tmdb_names(kind, ids):
    """TMDB's own names for company/network IDs, so a wrong ID shows up in the log."""
    return " + ".join(tmdb(f"/{kind}/{i}").get("name") or f"unknown {kind} {i}" for i in ids)


def pick_titles(data, entry):
    clean, only = split_ids(entry["ids"])
    kinds = [only] if only else ["movie", "tv"]
    note = ""

    if entry["type"] == "network":
        svc = resolve_service(data, entry)
        if svc:
            region, pids, pnames = svc
            trending = [r for r in data.trending
                        if r["kind"] in kinds and r["flatrate"].get(region, set()) & pids]
            # The service's own originals first, then other trending titles on it
            words, own_ids = ORIGINALS.get(clean[0], []), set(clean)

            def is_original(r):
                return bool(r["networks"] & own_ids) or \
                    any(w in m for w in words for m in r.get("makers", []))
            originals = [r for r in trending if is_original(r)]
            trending = originals + [r for r in trending if not is_original(r)]
            params = {"with_watch_providers": "|".join(map(str, sorted(pids))),
                      "watch_region": region, "with_watch_monetization_types": "flatrate"}
            filler = [x for k in kinds for x in data.discover(k, params)]
            note = f"{len(originals)} originals first, on {' / '.join(pnames)} in {region}"
        else:  # unknown service: fall back to "made by this network"
            ids = set(clean)
            trending = [r for r in data.trending if r["kind"] == "tv" and r["networks"] & ids]
            filler = data.discover("tv", {"with_networks": "|".join(clean)})
            note = "made by " + tmdb_names("network", clean)
    elif entry["type"] == "genre":
        g = dict(GENRES.get(entry["ids"][0], {}))   # genre names like "reality-tv" aren't split
        for k in ("movie", "tv"):
            if k in entry["opts"]:
                g[k] = [int(x) for x in re.findall(r"\d+", entry["opts"][k])]
        lang, not_lang = g.get("lang"), g.get("exclude_lang")
        need_all = entry["opts"].get("match", "").lower() == "all"   # e.g. romance AND comedy

        def genre_ok(kind, genres):
            want = set(g.get(kind) or [])
            return bool(want) and (want <= genres if need_all else bool(want & genres))

        def lang_ok(item_lang):
            return (not lang or item_lang == lang) and (not not_lang or item_lang != not_lang)
        trending = [r for r in data.trending
                    if genre_ok(r["kind"], r["genres"]) and lang_ok(r["lang"])]
        filler = []
        for k in ("movie", "tv"):
            if g.get(k):
                params = {"with_genres": ("," if need_all else "|").join(map(str, g[k])), "vote_count.gte": 20}
                if lang:
                    params["with_original_language"] = lang
                filler += [x for x in data.discover(k, params)
                           if lang_ok(x[1].get("original_language") or lang or "")]
        note = " + ".join(label for k, label in (("movie", "films"), ("tv", "TV")) if g.get(k)) + \
            (f", {lang} language only" if lang else "") + (", all genres must match" if need_all else "")
    else:  # company / studio
        ids = set(clean)
        trending = [r for r in data.trending if r["kind"] in kinds and r["companies"] & ids]
        filler = [x for k in kinds for x in data.discover(k, {"with_companies": "|".join(clean)})]
        note = "from " + tmdb_names("company", clean)

    titles, seen = [], set()
    if entry["type"] == "network":
        # 1st choice: the service's real Top 10 (FlixPatrol, via your AIOMetadata addon)
        top, top_region = service_top10(clean[0])
        if top:
            for kind, item in top:
                if kind in kinds and (kind, item["id"]) not in seen:
                    seen.add((kind, item["id"]))
                    titles.append((kind, item))
            note = f"{top_region} Top 10 first; " + note
        elif clean[0] == "213":   # Netflix fallback: Netflix's own weekly chart
            official, week = netflix_official(data)
            for kind, item in official:
                if kind in kinds and (kind, item["id"]) not in seen:
                    seen.add((kind, item["id"]))
                    titles.append((kind, item))
            if official:
                note = f"Netflix official {NETFLIX_COUNTRY} Top 10 (week of {week}) first; " + note
    n_official = len(titles)
    for r in trending:
        key = (r["kind"], r["item"]["id"])
        if key not in seen:
            seen.add(key)
            titles.append((r["kind"], r["item"]))
    n_trending = len(titles)
    filler.sort(key=lambda ki: ki[1].get("popularity", 0), reverse=True)
    for kind, item in filler:
        if len(titles) >= TITLES_PER_BACKDROP:
            break
        key = (kind, item["id"])
        if key not in seen:
            seen.add(key)
            titles.append((kind, item))
    head = titles[:3]
    top3 = ", ".join((i.get("name") or i.get("title") or "?") for _, i in head)
    return titles[:TITLES_PER_BACKDROP], f"{min(n_trending, TITLES_PER_BACKDROP)} trending this " \
        f"{TRENDING_WINDOW} + {max(0, len(titles) - n_trending)} popular ({note}). Top 3: {top3}"


# ── Bingecat ─────────────────────────────────────────────────────────

def get_json(url):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "prism-backdrops"})
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def catalog_url(base, cat, skip):
    parts = []
    for ex in cat.get("extra", []) or []:
        if ex.get("isRequired"):
            opts = ex.get("options") or []
            if not opts:
                return None  # e.g. search catalogs need a query: not a list
            parts.append(f"{ex['name']}={urllib.parse.quote(str(opts[0]))}")
    if skip:
        parts.append(f"skip={skip}")
    path = f"{base}/catalog/{cat['type']}/{urllib.parse.quote(cat['id'])}"
    return f"{path}/{'&'.join(parts)}.json" if parts else f"{path}.json"


def bingecat_titles(data, manifest_url, which, wanted_ids):
    base = manifest_url.split("?", 1)[0]
    if not base.endswith("/manifest.json"):
        raise ValueError("BINGECAT_MANIFEST_URL should end in manifest.json")
    base = base[: -len("/manifest.json")]
    stype = "movie" if which == "films" else "series"

    cats = [c for c in get_json(base + "/manifest.json").get("catalogs", []) if c.get("type") == stype]
    if wanted_ids:
        by_id = {c["id"]: c for c in cats}
        chosen = [by_id[i] for i in wanted_ids if i in by_id]
        for i in wanted_ids:
            if i not in by_id:
                log(f"    ! list {i} not found in your Bingecat addon")
    else:
        chosen = [c for c in cats if c.get("id", "").startswith(("aicat_cs_", "aicat_list_"))]
        chosen.sort(key=lambda c: "recommend" in c.get("name", "").lower())  # "Latest" lists first
    if not chosen:
        raise ValueError(f"no Bingecat {which} lists found")

    per_list = -(-TITLES_PER_BACKDROP // len(chosen))  # ceiling
    metas, seen = [], set()
    for cat in chosen:
        got, skip = 0, 0
        for _ in range(5):
            url = catalog_url(base, cat, skip)
            if not url:
                break
            page = get_json(url).get("metas", [])
            new = 0
            for m in page:
                mid = m.get("id")
                if mid and mid not in seen and got < per_list:
                    seen.add(mid)
                    metas.append(m)
                    got += 1
                    new += 1
            if not page or new == 0 or got >= per_list:
                break
            skip += len(page)

    kind = "movie" if stype == "movie" else "tv"
    titles = []
    for m in metas:
        mid, item = m["id"], None
        if mid.startswith("tt"):
            hits = tmdb(f"/find/{mid}", {"external_source": "imdb_id"}).get(f"{kind}_results") or []
            item = hits[0] if hits else None
        elif mid.startswith("tmdb:"):
            item = tmdb(f"/{kind}/{mid.split(':', 1)[1]}") or None
        if item and item.get("id") and not item.get("adult") and \
                (item.get("poster_path") or item.get("backdrop_path")):
            titles.append((kind, item))
    names = ", ".join(c.get("name", c["id"]) for c in chosen)
    return titles, f"{len(titles)} titles from your lists ({names})"


# ── Rendering (uses the prism-wallpapers flat T2 look) ───────────────

def screen_position(mod, cx, cy, ox, oy, W, H):
    """Where a point on the oversized canvas ends up in the final image
    (mirrors prism-wallpapers' flat mode: shift by OFFSET, then rotate by TILT)."""
    s = W / 1920.0
    sx, sy = ox - int(mod.OFFSET_X * s), oy - int(mod.OFFSET_Y * s)
    px, py = cx - sx - W / 2, cy - sy - H / 2          # relative to the crop centre
    a = math.radians(-mod.TILT_DEG)                    # PIL rotate() turns anticlockwise
    rx = px * math.cos(a) + py * math.sin(a)
    ry = -px * math.sin(a) + py * math.cos(a)
    return rx + W / 2, ry + H / 2


def prominence(mod, t, ox, oy, W, H):
    """How eye-catching a tile is in the finished backdrop: on screen, big, sharp, not darkened."""
    x, y = screen_position(mod, t["x"] + t["w"] / 2, t["y"] + t["h"] / 2, ox, oy, W, H)
    if not (0.06 * W < x < 0.96 * W and 0.08 * H < y < 0.92 * H):
        return 0.0
    # how much of the tile is on screen (corners inside the frame)
    corners = [screen_position(mod, t["x"] + dx, t["y"] + dy, ox, oy, W, H)
               for dx in (0, t["w"]) for dy in (0, t["h"])]
    inside = sum(1 for cx, cy in corners if 0 <= cx <= W and 0 <= cy <= H) / 4
    # sharp, central-right "hero" area (between the blur focus and the middle of the screen)
    hx, hy = (mod.DOF_FOCUS_X + 0.55) / 2 * W, (mod.DOF_FOCUS_Y + 0.5) / 2 * H
    dist = math.hypot(x - hx, y - hy) / math.hypot(W, H)
    focus = 1.0 - min(1.0, (dist * 1.6) ** 1.2)
    left = (240 / 255) * (1 - x / (0.65 * W)) ** 1.4 if x < 0.65 * W else 0.0
    bottom = (215 / 255) * ((y - 0.55 * H) / (0.45 * H)) ** 1.3 if y > 0.55 * H else 0.0
    return t["w"] * t["h"] * focus * (1 - left) * (1 - bottom) * t["opacity"] * inside ** 3


def build_ranked_layout(mod, ranked, W, H, scale):
    """Same grid as prism-wallpapers, but title #1 goes in the most prominent tile, #2 the next..."""
    lw = int(mod.LANDSCAPE_W * scale); lh = int(round(lw * 9 / 16))
    pw = int(mod.PORTRAIT_W * scale); ph = int(round(pw * 3 / 2))
    gap = int(mod.GAP * scale)
    rng = random.Random(42)
    bleed_x = (lw + gap) * 3
    bleed_y = max(ph, lh) * 2 + gap * 4

    columns, x, i = [], -bleed_x, 0
    while x < W + bleed_x:
        ct = mod.COL_PATTERN[i % len(mod.COL_PATTERN)]
        cw = lw if ct == "L" else pw
        columns.append({"x": x, "w": cw, "type": ct})
        x += cw + gap
        i += 1

    over_w, over_h, ox, oy = W + bleed_x * 2, H + bleed_y * 2, bleed_x, bleed_y
    canvas = Image.new("RGBA", (over_w, over_h), (10, 12, 16, 255))

    tiles = []
    for ci, col in enumerate(columns):
        depth = max(0.0, min(1.0, (col["x"] + col["w"] * 0.5) / W))
        opacity = mod.FADE_LEFT + (mod.FADE_RIGHT - mod.FADE_LEFT) * depth
        y = -bleed_y + (int(ph * mod.COL_STAGGER) if ci % 2 == 1 else 0) + oy
        while y < over_h:
            flip = rng.random() < mod.RANDOM_ASPECT_CHANCE
            ttype = ("P" if col["type"] == "L" else "L") if flip else col["type"]
            tw = col["w"]
            th = max(4, int(tw * 3 / 2)) if ttype == "P" else max(4, int(tw * 9 / 16))
            tiles.append({"x": col["x"] + ox, "y": y, "w": tw, "h": th, "type": ttype, "opacity": opacity})
            y += th + gap

    for t in tiles:
        t["score"] = prominence(mod, t, ox, oy, W, H)
    tiles.sort(key=lambda t: -t["score"])

    def image_for(entry, ttype):
        return (entry["port"] or entry["land"]) if ttype == "P" else (entry["land"] or entry["port"])

    # Visible tiles get titles strictly in rank order; the rest reuse lower-ranked titles.
    n = len(ranked)
    tail = ranked[n // 3:] or ranked
    for k, t in enumerate(tiles):
        entry = ranked[k] if k < n else tail[(k - n) % len(tail)]
        img = image_for(entry, t["type"])
        if img is not None:
            tile = mod._make_tile(img, t["w"], t["h"], opacity=t["opacity"])
            canvas.paste(tile, (int(t["x"]), int(t["y"])), tile)
    return canvas, ox, oy


def render(mod, titles, out_path):
    ranked = []
    for kind, item in titles:
        land = mod.resolve_image(kind, item, prefer_poster=False)
        port = mod.resolve_image(kind, item, prefer_poster=True)
        if land or port:
            ranked.append({"id": item["id"], "land": land, "port": port})
    if not ranked:
        raise RuntimeError("no images downloaded")

    over, ox, oy = build_ranked_layout(mod, ranked, 3840, 2160, scale=2.0)
    warped = mod._perspective_warp(over, ox, oy, 3840, 2160, mod.POV_X, mod.POV_Y)
    final = mod._apply_gradient(mod._apply_dof(warped, scale=2.0), mod._DEFAULT_ACCENT, True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".tmp.jpg")
    mod._save(final, str(tmp))
    tmp.replace(out_path)


def load_prism_module(work):
    path = work / "scripts" / "backdrop_T2_flat.py"
    spec = importlib.util.spec_from_file_location("prism_t2_flat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── README ───────────────────────────────────────────────────────────

def london_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London")).strftime("%d %B %Y, %H:%M")
    except Exception:
        return datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")


def write_readme(site, repo, entries, paths):
    lines = [
        "# Prism backdrops", "",
        "4K backdrops for Nuvio, rebuilt automatically every day from what's trending.",
        "Click the copy button on a link and paste it in. Links never change.", "",
        f"_Last updated {london_now()}_",
    ]
    for kind, (_, heading) in SECTIONS.items():
        rows = [(e, paths.get(id(e))) for e in entries if e["type"] == kind]
        rows = [(e, p) for e, p in rows if p]
        if not rows:
            continue
        lines += ["", f"## {heading}"]
        for e, rel in rows:
            lines += ["", f"**{e['name']}**", "```",
                      f"https://raw.githubusercontent.com/{repo}/main/{rel}", "```"]
    # Your own images (upload them into the images/ folder; subfolders become sub-headings)
    img_dir = site / "images"
    exts = {".webp", ".gif", ".png", ".jpg", ".jpeg", ".avif", ".svg"}
    images = sorted((p for p in img_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts),
                    key=lambda p: (str(p.parent.relative_to(img_dir)).lower(), p.name.lower())) \
        if img_dir.is_dir() else []
    if images:
        lines += ["", "## Images"]
        current = None
        for p in images:
            group = str(p.parent.relative_to(img_dir))
            if group != current and group != ".":
                lines += ["", f"### {group.replace('-', ' ').replace('_', ' ').title()}"]
            current = group
            rel = urllib.parse.quote(p.relative_to(site).as_posix())
            lines += ["", f'**{p.stem}**  ', f'<img src="{rel}" height="90">', "```",
                      f"https://raw.githubusercontent.com/{repo}/main/{rel}", "```"]
    lines += ["", "---", "",
              "Edit [backdrops.txt](backdrops.txt) to add, remove or reorder backdrops.",
              "Add your own images to the [images](images) folder, or add a line to "
              "[images/sources.txt](images/sources.txt) to copy one in from a link. "
              "Their links appear above after the next run.",
              "To rebuild now: **Actions** tab → **Daily backdrops** → **Run workflow**.", ""]
    (site / "README.md").write_text("\n".join(lines))


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--only", default="everything", choices=["everything", "services", "bingecat"])
    ap.add_argument("--parallel", type=int, default=3)
    args = ap.parse_args()

    work, site = Path(args.work).resolve(), Path(args.site).resolve()
    repo = os.environ.get("GITHUB_REPOSITORY", "xashujo/prism-backdrops")
    if not TMDB_KEY:
        sys.exit("✗ TMDB_API_KEY secret is missing. Add it under Settings → Secrets and variables → Actions.")

    entries = parse_config(site / "backdrops.txt")
    mod = load_prism_module(work)
    data = TMDBData(mod)
    paths, ok, failed = {}, [], []
    lock = threading.Lock()

    def dest_for(entry):
        section = SECTIONS[entry["type"]][0]
        if entry["type"] == "bingecat" and entry["opts"].get("profile"):
            return site / section / f"{entry['ids'][0]}-p{entry['opts']['profile']}"
        if entry["type"] in ("bingecat", "genre"):
            return site / section / entry["ids"][0]
        return find_folder(site / section, entry["ids"][0]) or \
            site / section / f"{entry['ids'][0]}-{slugify(entry['name'])}"

    def record_existing(entry):
        d = dest_for(entry)
        if (d / IMAGE).exists():
            paths[id(entry)] = f"{SECTIONS[entry['type']][0]}/{d.name}/{IMAGE}"

    def build(entry):
        try:
            if entry["type"] == "bingecat":
                manifest = os.environ.get("BINGECAT_MANIFEST_URL", "").strip()
                if not manifest:
                    raise RuntimeError("BINGECAT_MANIFEST_URL secret not set")
                if entry["opts"].get("profile"):   # another person's Bingecat profile on the same account
                    manifest = re.sub(r"(/stremio/[^/]+)/(\d+/)?", rf"\g<1>/{entry['opts']['profile']}/", manifest, count=1)
                titles, note = bingecat_titles(data, manifest, entry["ids"][0], entry["ids"][1:])
            else:
                titles, note = pick_titles(data, entry)
            if len(titles) < 8:
                raise RuntimeError(f"only {len(titles)} titles found")
            d = dest_for(entry)
            render(mod, titles, d / IMAGE)
            with lock:
                ok.append((entry, note))
            log(f"  ✓ {entry['name']}: {note}")
        except Exception as exc:
            with lock:
                failed.append((entry, str(exc)))
            log(f"  ✗ {entry['name']}: {exc}")
        record_existing(entry)

    services = [e for e in entries if e["type"] != "bingecat"]
    bingecat = [e for e in entries if e["type"] == "bingecat"]
    todo = []
    if args.only in ("everything", "services"):
        todo += services
    if args.only in ("everything", "bingecat"):
        todo += bingecat
    for e in entries:
        if e not in todo:
            record_existing(e)

    if any(e["type"] != "bingecat" for e in todo):
        log(f"== Loading what's trending this {TRENDING_WINDOW} ==")
        data.load_trending()
    log(f"== Building {len(todo)} backdrop(s), {args.parallel} at a time ==")
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(build, todo))

    write_readme(site, repo, entries, paths)

    order = {id(e): i for i, e in enumerate(entries)}
    summary = [f"### {len(ok)} backdrop(s) rebuilt", ""]
    summary += [f"- ✓ **{e['name']}**: {note}" for e, note in sorted(ok, key=lambda x: order[id(x[0])])]
    if failed:
        summary += ["", "**Not rebuilt this time** (previous image kept where there was one):"]
        summary += [f"- ✗ **{e['name']}**: {why}" for e, why in sorted(failed, key=lambda x: order[id(x[0])])]
    log("\n" + "\n".join(summary))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write("\n".join(summary) + "\n")

    if not ok:
        sys.exit("✗ Nothing was rebuilt. Check the log above.")


if __name__ == "__main__":
    main()
