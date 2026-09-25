# My_Map satellite data mirror

A scheduled republisher of CelesTrak satellite data for the macOS app **My_Map**.

## Why this exists

CelesTrak publishes orbital element data freely and asks, reasonably, that a given dataset be
downloaded no more than once per its update cycle. Since March 2026 it enforces that on
high-traffic groups: a second request inside the same 2-hour window gets HTTP 403, and its
[usage policy](https://celestrak.org/usage-policy.php) is explicit that software which keeps
querying after a non-200 response "will end up sending your IP address to the firewall."

An app that ships to users cannot honour that per-user. Each copy sees only its own requests, and
users behind a shared ISP address (CGNAT) can be refused because of a stranger's copy. CelesTrak
has no paid tier and has said it will not have one, so there is nothing to buy instead.

So this repository fetches once per cycle and republishes. CelesTrak sees exactly one
well-behaved client; My_Map reads from GitHub Pages.

## What it publishes

| File | Contents |
|---|---|
| `docs/visual.tle` | CelesTrak's curated "100 (or so) Brightest" group, byte-identical to what CelesTrak served. Nothing is prepended — SatelliteKit's `preProcessTLEs(_:)` expects exact three-line groups, so even a comment header would break parsing. |
| `docs/satcat-visual.csv` | SATCAT reference rows (launch date, owner, orbit) for only the objects in `visual.tle`, with CelesTrak's header and column order preserved. About 15 KB instead of the full catalog's ~7 MB. |
| `docs/status.json` | When the data last *changed*, and how much of it there is. The Actions run history is the record of when it was last *checked*. |

## How it behaves

**It validates before publishing.** A truncated transfer or an HTML error page written over a good
`visual.tle` would break every user's sky dome at once, and they would cache the broken file. So
payloads are checked in memory — three-line structure, object-count floor, catalog-row floor, no
HTML — and a failed check leaves the published files untouched.

**It only writes when the content differs.** No change means no commit, which means no Pages
redeploy, which means the ETag the app is holding stays valid and its next request costs a 304 of
a few hundred bytes instead of a download. Rewriting identical files every two hours would defeat
the app's caching entirely.

**It does not retry a refusal.** One request per dataset per run. If a cycle fails the run goes
red and the next attempt is two hours away; the app keeps serving its own cache meanwhile.

## Attribution

The orbital and catalog data republished here originates with the US Space Force and is
distributed via [Space-Track.org](https://www.space-track.org), which grants blanket approval to
redistribute basic space situational awareness data when accompanied by appropriate citation. It
is retrieved here from [CelesTrak](https://celestrak.org).

The same citation belongs in the app's About/Credits panel, since that is the condition the
approval carries:

> Satellite orbital data courtesy of CelesTrak (celestrak.org), derived from data provided by the
> United States Space Force via Space-Track.org.

## Setup

1. Push this repository (public).
2. **Settings → Pages → Deploy from a branch**, branch `main`, folder `/docs`.
3. Set `PROJECT_URL` in `scripts/mirror_fetch.py`. `CONTACT` is optional and left empty here;
   if you ever add one, use a forwarding alias, not a personal address — this file is public.
4. **Actions → Mirror satellite data → Run workflow** to populate `docs/` immediately.
5. Point the app at the published URLs:
   - `SatelliteCatalog.mirrorURL` → `https://jimsbeam.github.io/my-map-satellite-mirror/visual.tle`
   - `SatelliteReferenceCatalog.sourceURL` → `https://jimsbeam.github.io/my-map-satellite-mirror/satcat-visual.csv`

## Keeping an eye on it

GitHub disables scheduled workflows after 60 days of repository inactivity, and a bot's own
commits do not reliably reset that clock. A warning email arrives first; the schedule is
re-enabled from the Actions tab.

The failure is deliberately gentle — My_Map falls back to its disk cache and tells the user the
elements are ageing — but it is silent from the outside, so a calendar reminder or a check on
`docs/status.json` is worth having.
