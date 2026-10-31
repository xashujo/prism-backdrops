#!/usr/bin/env python3
"""
make-it-current.py — changes how backdrop_T2_flat.py picks titles so the
backdrops show what's popular RIGHT NOW on each service/studio.

  Before: TMDB's 60 most popular titles of all time-ish, with a weak
          "newer is better" boost (anything from last year counts as new).
  After:  1. Shows that have aired an episode in the last 6 months and
             films released in the last 24 months, ranked by popularity.
             These always take the sharp, in-focus area.
          2. If a studio/service doesn't have enough recent titles
             (e.g. Studio Ghibli), the rest is topped up with its
             all-time popular titles, so backdrops never come out empty.

It edits ~/prism-wallpapers/scripts/backdrop_T2_flat.py and keeps the
original as backdrop_T2_flat.py.orig. Safe to run twice.
To undo:  mv scripts/backdrop_T2_flat.py.orig scripts/backdrop_T2_flat.py
"""
import re
import shutil
import sys
from pathlib import Path

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path.home() / "prism-wallpapers" / "scripts" / "backdrop_T2_flat.py"

if not TARGET.exists():
    sys.exit(f"✗ Can't find {TARGET}")

src = TARGET.read_text()
if "RECENT_TV_MONTHS" in src:
    print("✓ Already switched to 'current' mode. Nothing to do.")
    sys.exit(0)

CONFIG = '''
# -- "Current" mode: favour what's popular right now (make-it-current patch)
RECENT_TV_MONTHS = 6      # shows that aired an episode in the last N months
RECENT_MOVIE_MONTHS = 24  # films released in the last N months
'''

NEW_FUNCS = '''# ── "Current" mode (make-it-current patch) ─────────────────────────────
from datetime import date as _date, timedelta as _timedelta


def _months_ago(months):
    return (_date.today() - _timedelta(days=int(months * 30.44))).isoformat()


def _mark_recent(items):
    for _, item in items:
        item["_recent"] = True
    return items


def _top_up(recent, everything, count):
    """Recent titles first, then all-time popular ones not already included."""
    seen = {item["id"] for _, item in recent}
    rest = [ki for ki in everything if ki[1]["id"] not in seen]
    return (recent + rest)[:count]


def _pull_tv(extra, count):
    today = _date.today().isoformat()
    recent = _mark_recent(_pull_tv_all({
        **extra,
        "air_date.gte": _months_ago(RECENT_TV_MONTHS),
        "air_date.lte": today,
    }, count))
    if len(recent) >= count:
        return recent[:count]
    return _top_up(recent, _pull_tv_all(extra, count), count)


def _pull_movies(extra, count):
    today = _date.today().isoformat()
    recent = _mark_recent(_pull_movies_all({
        **extra,
        "primary_release_date.gte": _months_ago(RECENT_MOVIE_MONTHS),
        "primary_release_date.lte": today,
    }, count))
    if len(recent) >= count:
        return recent[:count]
    return _top_up(recent, _pull_movies_all(extra, count), count)


def _calculate_focal_score(item):
    """Current titles first (pure popularity), then older top-ups
    (popularity weighted toward newer releases)."""
    pop = float(item.get("popularity", 0))
    if item.get("_recent"):
        return (1, pop)

    date_str = item.get("release_date") or item.get("first_air_date") or ""
    if not date_str:
        return (0, pop)
    try:
        release_year = datetime.strptime(date_str, "%Y-%m-%d").year
        age = max(1, datetime.now().year - release_year)
        return (0, pop / (age ** 0.5))
    except ValueError:
        return (0, pop)


'''


def must_sub(pattern, repl, text, what, flags=0):
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        sys.exit(f"✗ Couldn't find {what} in the script (it may have changed). Nothing was modified.")
    return new


out = src

# 1. Settings, just after FETCH_COUNT
out = must_sub(r"(^FETCH_COUNT\s*=.*$)", lambda m: m.group(1) + "\n" + CONFIG.rstrip("\n"),
               out, "the FETCH_COUNT setting", flags=re.M)

# 2. Keep the original fetchers under new names
out = must_sub(r"^def _pull_tv\(extra, count\):", "def _pull_tv_all(extra, count):",
               out, "_pull_tv", flags=re.M)
out = must_sub(r"^def _pull_movies\(extra, count\):", "def _pull_movies_all(extra, count):",
               out, "_pull_movies", flags=re.M)

# 3. Replace the scoring function with the new fetchers + scoring
out = must_sub(r"^def _calculate_focal_score\(item\):.*?(?=^def fetch_titles\()",
               lambda m: NEW_FUNCS, out, "_calculate_focal_score", flags=re.M | re.S)

# 4. When TV + films are combined (studios), keep recent ones first
out = must_sub(
    r'combined_sorted = sorted\(combined, key=lambda kt: kt\[1\]\.get\("popularity", 0\), reverse=True\)',
    'combined_sorted = sorted(combined, key=lambda kt: (1 if kt[1].get("_recent") else 0, '
    'kt[1].get("popularity", 0)), reverse=True)',
    out, "the TV + film merge step")

# Sanity check: the edited script must still be valid Python
try:
    compile(out, str(TARGET), "exec")
except SyntaxError as e:
    sys.exit(f"✗ Edit produced invalid code ({e}). Nothing was modified.")

backup = TARGET.with_suffix(".py.orig")
if not backup.exists():
    shutil.copy2(TARGET, backup)
TARGET.write_text(out)

print("✓ Switched to 'current' mode")
print("  Shows: aired an episode in the last 6 months · Films: released in the last 24 months")
print("  Topped up with all-time popular titles where there aren't enough.")
print(f"  Original kept as {backup.name}")
