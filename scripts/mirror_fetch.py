#!/usr/bin/env python3
"""
mirror_fetch.py — republish CelesTrak satellite data for My_Map.

This runs in GitHub Actions, not in the app. It is the single well-behaved client that talks to
CelesTrak: once per 2-hour update cycle, one request per dataset, no retries on a refusal. Every
copy of My_Map then reads the republished copy from GitHub Pages, so CelesTrak never sees more
than this one job no matter how many copies of the app exist.

What it publishes into docs/ (the GitHub Pages root):

    visual.tle          CelesTrak's curated "100 (or so) Brightest" group, byte-identical to what
                        CelesTrak served. SatelliteKit's preProcessTLEs(_:) expects exactly this
                        three-line layout, so nothing — not even a comment header — may be added.
                        Attribution lives in README.md and the app's About panel instead.

    satcat-visual.csv   The SATCAT rows for just the objects present in visual.tle, with CelesTrak's
                        header and column order preserved. Same format the full catalog uses, so the
                        app's existing CSV parser needs no change — but ~15 KB instead of ~7 MB,
                        which also keeps this repository from growing by megabytes a day.

    status.json         When the data last actually changed, and how much of it there is. Useful for
                        a monitor; not read by the app.

TWO RULES THIS SCRIPT FOLLOWS, AND WHY

1.  Never publish an unvalidated payload. A truncated response or an HTML error page written over
    a good visual.tle would break the Satellites dome for every user at once, and they would keep
    that broken file cached. So everything is validated in memory first, and a failed validation
    leaves the previously published files exactly as they were.

2.  Only write when the content actually differs. An unchanged file is not rewritten, so no commit
    happens, so GitHub Pages does not redeploy, so the ETag the app holds stays valid and the app's
    conditional GET keeps answering 304 in a few hundred bytes. Rewriting identical files every two
    hours would quietly defeat the whole caching design on the app side.

Exit codes: 0 = published or nothing to do; 1 = something went wrong and nothing was changed.
A non-zero exit turns the Actions run red, which is how you find out the mirror has stopped.
"""

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------------------------
# Configuration

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle"
SATCAT_URL = "https://celestrak.org/pub/satcat.csv"

OUTPUT_DIR = "docs"
GP_OUTPUT = os.path.join(OUTPUT_DIR, "visual.tle")
SATCAT_OUTPUT = os.path.join(OUTPUT_DIR, "satcat-visual.csv")
STATUS_OUTPUT = os.path.join(OUTPUT_DIR, "status.json")

#  CelesTrak asks that clients identify themselves, and a contactable agent string is the
#  difference between being emailed about a problem and being firewalled over one. It does NOT
#  have to be a personal address, and it should not be: this file is public, and a public repo is
#  a scraped repo. A forwarding alias that lands in the same inbox does the job and can be thrown
#  away if it starts attracting spam. Leaving the contact out entirely is also allowed — the URL
#  alone identifies the client — it only means a problem arrives as a block rather than a note.
CONTACT = ""                      #  e.g. "mymap-mirror@example.com", or leave empty
PROJECT_URL = "https://github.com/jimsBeam/my-map-satellite-mirror"   #  set once the mirror has a home

USER_AGENT = (
    f"My_Map-mirror/1.0 (+{PROJECT_URL}; {CONTACT})" if CONTACT
    else f"My_Map-mirror/1.0 (+{PROJECT_URL})"
)

REQUEST_TIMEOUT = 30

#  Sanity floors. The visual group normally carries well over a hundred objects and the full SATCAT
#  well over sixty thousand rows. Anything far below that is a truncated transfer, not a real update.
MIN_GP_OBJECTS = 50
MIN_SATCAT_ROWS = 20_000

#  SATCAT column order, per https://celestrak.org/satcat/satcat-format.php
SATCAT_NORAD_COLUMN = 2


class MirrorError(Exception):
    """Something went wrong. Nothing on disk has been touched."""


# ---------------------------------------------------------------------------------------------
# Fetching


def fetch(url, label):
    """One request. No retries — CelesTrak's usage policy is explicit that repeating a refused
    request is what gets an address firewalled, and a scheduled job has nothing to gain by
    hurrying. If this cycle fails, the next one is two hours away and the app is still serving
    its cache in the meantime."""

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    })

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
            print(f"  {label}: HTTP {response.status}, {len(raw):,} bytes")
            return raw.decode("utf-8", errors="strict")

    except urllib.error.HTTPError as error:
        if error.code in (403, 404, 410):
            raise MirrorError(
                f"{label}: HTTP {error.code}. Repeating this request cannot change the answer. "
                f"Check the URL against CelesTrak's current documentation, and check whether this "
                f"job is running more than once per update cycle."
            )
        raise MirrorError(f"{label}: HTTP {error.code} {error.reason}")

    except urllib.error.URLError as error:
        raise MirrorError(f"{label}: could not reach CelesTrak ({error.reason})")

    except UnicodeDecodeError:
        raise MirrorError(f"{label}: response was not valid UTF-8")


# ---------------------------------------------------------------------------------------------
# Validation


def looks_like_html(text):
    head = text[:2000].lower()
    return "<html" in head or "<!doctype html" in head


def validate_gp(text):
    """Returns (cleaned_text, object_count, norad_ids). Raises if the payload is not a well-formed
    three-line TLE set."""

    if looks_like_html(text):
        raise MirrorError("GP data: got an HTML page, not TLE text")

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]

    if not lines:
        raise MirrorError("GP data: empty")

    if len(lines) % 3 != 0:
        raise MirrorError(
            f"GP data: {len(lines)} non-blank lines is not a multiple of 3 — the transfer was "
            f"probably truncated"
        )

    norad_ids = set()

    for index in range(0, len(lines), 3):
        line1, line2 = lines[index + 1], lines[index + 2]

        if not line1.startswith("1 ") or not line2.startswith("2 "):
            raise MirrorError(
                f"GP data: malformed element set at line {index + 1} "
                f"({lines[index][:24]!r})"
            )

        #  Catalog number, TLE columns 3-7, zero-padded. SATCAT publishes it unpadded, so
        #  normalise here or the two will never match. Alpha-5 designators (a letter in the
        #  first column, used once five-digit numbers ran out) are kept as-is.
        raw_id = line1[2:7].strip()
        try:
            norad_ids.add(str(int(raw_id)))
        except ValueError:
            norad_ids.add(raw_id)

    count = len(lines) // 3

    if count < MIN_GP_OBJECTS:
        raise MirrorError(
            f"GP data: only {count} objects, below the floor of {MIN_GP_OBJECTS}. Refusing to "
            f"publish over a good file."
        )

    #  Republished with a trailing newline and nothing else changed.
    return "\n".join(lines) + "\n", count, norad_ids


def filter_satcat(text, norad_ids):
    """Returns (csv_text, row_count). Keeps CelesTrak's header and column order, and only the rows
    for objects that appear in the current visual group."""

    if looks_like_html(text):
        raise MirrorError("SATCAT: got an HTML page, not CSV")

    reader = csv.reader(io.StringIO(text))

    try:
        header = next(reader)
    except StopIteration:
        raise MirrorError("SATCAT: empty")

    if "NORAD_CAT_ID" not in header:
        raise MirrorError(f"SATCAT: unexpected header {header[:6]} — column layout has changed")

    kept, total = [], 0

    for row in reader:
        total += 1
        if len(row) <= SATCAT_NORAD_COLUMN:
            continue
        raw_id = row[SATCAT_NORAD_COLUMN].strip()
        try:
            normalised = str(int(raw_id))
        except ValueError:
            normalised = raw_id
        if normalised in norad_ids:
            kept.append(row)

    if total < MIN_SATCAT_ROWS:
        raise MirrorError(
            f"SATCAT: only {total:,} rows, below the floor of {MIN_SATCAT_ROWS:,}. Refusing to "
            f"publish over a good file."
        )

    if not kept:
        raise MirrorError("SATCAT: no rows matched the visual group — check the NORAD column index")

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(kept)

    print(f"  SATCAT: {total:,} rows scanned, {len(kept)} kept for the visual group")
    return buffer.getvalue(), len(kept)


# ---------------------------------------------------------------------------------------------
# Writing


def write_if_changed(path, text):
    """True if the file was written, False if the content was already identical.

    The False case is the important one: it means no commit, so no Pages redeploy, so the ETag the
    app is holding stays valid and its next request costs a 304 instead of a download."""

    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as existing:
            if existing.read() == text:
                print(f"  {path}: unchanged")
                return False

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    print(f"  {path}: written ({len(text.encode('utf-8')):,} bytes)")
    return True


# ---------------------------------------------------------------------------------------------


def main():
    print("Fetching from CelesTrak")

    try:
        gp_raw = fetch(GP_URL, "GP data")
        gp_text, object_count, norad_ids = validate_gp(gp_raw)

        satcat_raw = fetch(SATCAT_URL, "SATCAT")
        satcat_text, satcat_rows = filter_satcat(satcat_raw, norad_ids)

    except MirrorError as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        print("Nothing was changed. The previously published files are still being served, and "
              "the app will keep serving its own cache.", file=sys.stderr)
        return 1

    print("\nPublishing")
    changed = write_if_changed(GP_OUTPUT, gp_text)
    changed |= write_if_changed(SATCAT_OUTPUT, satcat_text)

    if not changed:
        print("\nNothing changed this cycle. No commit, no redeploy, app ETags stay valid.")
        return 0

    #  Written only when the data changed, for the same ETag-stability reason. The Actions run
    #  history is the record of when the mirror last *checked*; this is when it last *changed*.
    status = {
        "data_changed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "visual_object_count": object_count,
        "satcat_rows_published": satcat_rows,
        "source": "CelesTrak (https://celestrak.org) — data derived from US Space Force / "
                  "Space-Track.org, republished with citation",
    }
    write_if_changed(STATUS_OUTPUT, json.dumps(status, indent=2) + "\n")

    print(f"\nPublished {object_count} objects and {satcat_rows} catalog rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
