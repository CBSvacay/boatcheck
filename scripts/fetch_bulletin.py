#!/usr/bin/env python3
"""
Fetch Environment Canada marine bulletins and write data/bulletin.json.

Why this exists
---------------
The old feed at weather.gc.ca/rss/marine/{id}_e.xml is gone. Environment
Canada still publishes the same bulletins, but on the MSC Datamart, which is
built for programs rather than browsers. Two things make it awkward to read
directly from a phone:

  * the files live in folders named by UTC hour, and those folders are
    deleted as the day rolls over, so there is no fixed address
  * the browser may refuse to read another site's data at all

Running it here sidesteps both. GitHub fetches the bulletin on a schedule and
commits the result into the repo, so the page reads it from its own address
and nothing can block it.

Output shape matches what index.html already expected from the old feed, so
the page needs only a loader, not a new parser.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# The datamart exposes the same tree several ways and the aliases are not
# equally reliable. "/today/" is a convenience symlink that has been seen to
# 404 while the dated path underneath it works fine, so every known layout is
# tried in turn and whichever answers first wins.
#
# Dated paths are built for both today and yesterday UTC, because the rollover
# happens mid-afternoon Pacific time and the new folder can be empty for a
# while after it appears.
# Bulletins are issued every six hours (04, 10, 16, 22 UTC) plus amendments,
# so six hours old is normal. Ten means a scheduled issue was missed.
STALE_AFTER_H = 10
TIMEOUT = 12          # per request; a server that hasn't answered by now won't
DEADLINE_S = 180      # hard stop for the whole run, so it can never hang a job
UA = {"User-Agent": "Mozilla/5.0 (compatible; boatcheck/1.0; marine bulletin fetcher)",
      "Accept": "*/*"}
_started = None


class OutOfTime(Exception):
    pass


def check_clock():
    if _started is not None and (datetime.now(timezone.utc) - _started).total_seconds() > DEADLINE_S:
        raise OutOfTime("gave up after %d seconds" % DEADLINE_S)


def last_working_base():
    """
    The log showed six canonical addresses failing before the backup answered,
    which is where the three minutes went. Whichever one worked last time is
    recorded in the output file and tried first, so a repeat run is quick.
    """
    try:
        with open("data/bulletin.json", encoding="utf-8") as f:
            return json.load(f).get("base_used")
    except Exception:
        return None


def candidate_bases():
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    days = [now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")]
    out = []
    # Dated paths first: they are the real structure. "/today/" is only a
    # symlink over the top of them and has been seen to 404 on its own.
    for host in ("https://dd.weather.gc.ca", "https://dd.meteo.gc.ca"):
        for d in days:
            out.append("%s/%s/WXO-DD/marine_weather/pacific" % (host, d))
    out += ["https://dd.weather.gc.ca/today/marine_weather/pacific",
            "https://dd.meteo.gc.ca/today/marine_weather/pacific"]
    # hpfx is explicitly "best effort" with no redundancy, so it is the last
    # resort rather than an early try that can stall the run.
    for d in days:
        out.append("https://hpfx.collab.science.gc.ca/%s/WXO-DD/marine_weather/pacific" % d)

    # Promote last time's winner, but keep the rest of the list behind it so a
    # server that has since gone down doesn't strand us.
    prev = last_working_base()
    if prev:
        today_prev = re.sub(r"/\d{8}/", "/%s/" % days[0], prev)
        # Insert in reverse so today's dated path ends up ahead of yesterday's.
        for c in (prev, today_prev):
            if c in out:
                out.remove(c)
            out.insert(0, c)
    seen, ordered = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); ordered.append(u)
    return ordered


BASE = None      # settled by hour_dirs() once a layout answers

# Site codes come from https://dd.weather.gc.ca/today/marine_weather/regionList.xml
#
# The datamart publishes one file per whole strait, with the sub-areas inside
# it as separate <location> elements. "match" picks the sub-area this app
# actually cares about; None means the file has only one and we take it.
ZONES = {
    "haro": {"code": "m0000064", "label": "Haro Strait", "match": None},
    "jdf":  {"code": "m0000009", "label": "Juan de Fuca Strait — east entrance",
             "match": "east entrance"},
    "sog":  {"code": "m0000028", "label": "Strait of Georgia — south of Nanaimo",
             "match": "south of nanaimo"},
}


_listing_cache = {}


def get(url):
    check_clock()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def listing(url):
    """Directory listings are re-read once per zone otherwise — same folder,
    same answer, three times over."""
    if url not in _listing_cache:
        _listing_cache[url] = get(url).decode("utf-8", "replace")
    return _listing_cache[url]


def hour_dirs():
    """
    Find a working base and return its hour folders, newest first.

    Every candidate is reported either way, so a future breakage shows up in
    the log as a list of what was tried rather than a bare 404.
    """
    global BASE
    tried = []
    for base in candidate_bases():
        try:
            html = listing(base + "/")
        except urllib.error.HTTPError as e:
            tried.append("%s -> HTTP %s" % (base, e.code));  continue
        except Exception as e:
            tried.append("%s -> %s" % (base, e));            continue
        hours = sorted(set(re.findall(r'href="(\d{2})/"', html)), reverse=True)
        if hours:
            BASE = base
            print("using %s" % base)
            return hours
        tried.append("%s -> listing had no hour folders" % base)
    raise RuntimeError("no working datamart path. Tried:\n  " + "\n  ".join(tried))


def published_iso(url):
    """
    When the datamart published this file, read from its own filename
    (20260830T100026.074Z_MSC_...).

    This is the honest measure of freshness. The timestamp inside the file is
    called lastModifiedTime, and if Environment Canada reissues a forecast
    whose content hasn't changed, that field may well not move — which would
    make a perfectly current bulletin look ancient. The filename always
    changes, because a new file is written at every issue time.
    """
    m = re.search(r"/(\d{8})T(\d{6})", url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def find_file(hours, code):
    """
    Walk back from the newest hour until this site code turns up.

    Older folders are kept as fallbacks rather than insisting on the newest,
    because a folder can exist before every region has been written into it.
    """
    for hh in hours:
        try:
            html = listing("%s/%s/" % (BASE, hh))
        except Exception:
            continue
        names = re.findall(r'href="([^"]*_MSC_MarineWeather_%s_en\.xml)"' % code, html)
        if names:
            return "%s/%s/%s" % (BASE, hh, sorted(names)[-1])
    return None


def text_of(el):
    return " ".join((el.text or "").split()) if el is not None else ""


def pick_location(parent, match):
    """The sub-area we want, by its name attribute."""
    locs = parent.findall("location") if parent is not None else []
    if not locs:
        return None
    if match:
        for loc in locs:
            if match in (loc.get("name") or "").lower():
                return loc
    return locs[0]


def join_sentences(bits):
    """Join without doubling the full stops the source already supplies."""
    out = []
    for b in bits:
        b = b.strip()
        if b:
            out.append(b if b.endswith((".", "!", "?")) else b + ".")
    return " ".join(out)


def regular_text(loc):
    """
    Flatten a forecast into one sentence blob.

    index.html scans this for wind figures and day names, exactly as it did
    with the old feed, so the shape matters more than the prettiness.

    Wind and visibility ONLY. Air temperature is deliberately left out: the
    page treats every number here as a wind speed, so "Air temperature 22"
    would be read as 22 knots and could turn a fine day red. Temperature is
    kept separately below, and the page gets its own from the wind model
    anyway.
    """
    if loc is None:
        return ""
    parts = []
    for wc in loc.findall("weatherCondition"):
        period = text_of(wc.find("periodOfCoverage"))
        bits = [text_of(wc.find(t)) for t in ("wind", "weatherVisibility")]
        body = join_sentences(bits)
        if body:
            parts.append((period + ": " if period else "") + body)
    return " ".join(parts)


def side_notes(loc):
    """Air temperature, freezing spray and any status message, kept out of the
    text the wind scanner reads."""
    if loc is None:
        return ""
    bits = []
    for wc in loc.findall("weatherCondition"):
        bits += [text_of(wc.find(t)) for t in ("airTemperature", "freezingSpray")]
    bits.append(text_of(loc.find("statusStatement")))
    return join_sentences(bits)


def extended_text(loc):
    if loc is None:
        return ""
    parts = []
    for wc in loc.findall("weatherCondition"):
        for fp in wc.findall("forecastPeriod"):
            name = fp.get("name") or ""
            body = text_of(fp)
            if body:
                parts.append((name + ": " if name else "") + body)
    return " ".join(parts)


def wave_text(loc):
    if loc is None:
        return ""
    parts = []
    for wc in loc.findall("weatherCondition"):
        period = text_of(wc.find("periodOfCoverage"))
        body = text_of(wc.find("textSummary"))
        if body:
            parts.append((period + ": " if period else "") + body)
    return " ".join(parts)


def warnings_for(root, match):
    """
    Alerts in effect. An empty list means none — which is information, not a
    failure, and the page needs to be able to tell those apart.
    """
    out = []
    wr = root.find("warnings")
    if wr is None:
        return out
    locs = wr.findall("location")
    for loc in locs:
        name = (loc.get("name") or "").lower()
        # A warning on the parent strait applies unless it names a different
        # sub-area. Better to over-report an alert than to miss one.
        if match and name and match not in name and len(locs) > 1:
            continue
        for ev in loc.findall("event"):
            title = ev.get("name") or ev.get("type") or "Alert"
            cat = (ev.get("category") or ev.get("type") or "warning")
            body = text_of(ev) or title
            out.append({"title": title.strip(), "cat": cat.strip(), "text": body})
    return out


def _stamp_to_iso(ts):
    ts = (ts or "").strip()
    if len(ts) == 12 and ts.isdigit():
        try:
            return datetime.strptime(ts, "%Y%m%d%H%M").replace(
                tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def issued_iso(root):
    """
    When Environment Canada issued this bulletin.

    The published tag table lists dateTime under both marineData and
    lastModifiedTime, and each forecast section carries its own, so the exact
    nesting varies. Rather than assume one path, take every timeStamp in the
    file and use the most recent that isn't in the future — that is the issue
    time whichever way the file is arranged.
    """
    stamps = []
    for el in root.iter("timeStamp"):
        iso = _stamp_to_iso(el.text)
        if iso:
            stamps.append(iso)
    if not stamps:
        return None
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    past = [x for x in stamps if x <= now]
    return max(past) if past else min(stamps)


def outline(root, depth=2):
    """Tag structure, for the log, when a timestamp can't be found. Beats
    guessing at the layout from a documentation table."""
    lines = []

    def walk(el, d, path):
        if d > depth:
            return
        kids = list(el)
        tags = sorted({k.tag for k in kids})
        if tags:
            lines.append("    %s> %s" % (path, ", ".join(tags)))
        for t in tags:
            walk([k for k in kids if k.tag == t][0], d + 1, path + "/" + t)

    walk(root, 0, root.tag)
    return "\n".join(lines)


def build_zone(key, spec, hours):
    url = find_file(hours, spec["code"])
    if not url:
        raise RuntimeError("no current file for %s (%s)" % (key, spec["code"]))
    root = ET.fromstring(get(url))

    iss = issued_iso(root)
    if iss is None and not build_zone.__dict__.get("shown"):
        build_zone.__dict__["shown"] = True
        print("  no timeStamp found — actual file structure:")
        print(outline(root))

    reg = pick_location(root.find("regularForecast"), spec["match"])
    ext = pick_location(root.find("extendedForecast"), spec["match"])
    wav = pick_location(root.find("waveForecast"), spec["match"])

    forecast = regular_text(reg)
    if not forecast:
        raise RuntimeError("empty forecast text for " + key)

    z = {
        "label": spec["label"],
        "area": (reg.get("name") if reg is not None else "") or spec["label"],
        "issued": iss,
        "published": published_iso(url),
        "source": url,
        "warnings": warnings_for(root, spec["match"]),
        # Only wind and visibility go in .text — see regular_text().
        "forecast": {"text": forecast},
        "extended": {"text": extended_text(ext)},
        "waves": wave_text(wav),
        "notes": side_notes(reg),
    }
    return z


def main():
    global _started
    _started = datetime.now(timezone.utc)
    try:
        hours = hour_dirs()
    except Exception as e:
        print(e, file=sys.stderr)
        return 1
    print("hour folders available:", ", ".join(hours))

    zones, failed = {}, {}
    for key, spec in ZONES.items():
        try:
            zones[key] = build_zone(key, spec, hours)
            w = len(zones[key]["warnings"])
            when = zones[key].get("published") or zones[key].get("issued")
            age = ""
            if when:
                try:
                    hrs = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(when.replace("Z", "+00:00"))
                           ).total_seconds()/3600
                    age = ", published %.1f h ago" % hrs
                    # Scheduled issues are six hours apart (04, 10, 16, 22), so
                    # six hours old is normal right before the next one. Ten
                    # hours means a whole cycle was missed or the server we
                    # reached is lagging — that is worth flagging, six is not.
                    if hrs > STALE_AFTER_H:
                        age += "  <-- STALE, a scheduled issue was missed"
                except Exception:
                    pass
            print("  %-5s ok  — %s%s%s" % (
                key, zones[key]["area"],
                (", %d alert(s)" % w) if w else ", no alerts", age))
        except OutOfTime as e:
            failed[key] = str(e)
            print("  %-5s FAILED — %s" % (key, e), file=sys.stderr)
            break
        except Exception as e:
            failed[key] = str(e)
            print("  %-5s FAILED — %s" % (key, e), file=sys.stderr)

    if not zones:
        print("every zone failed; leaving the previous file untouched", file=sys.stderr)
        return 1

    out = {
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_used": BASE,
        "zones": zones,
    }
    if failed:
        out["failed"] = failed

    os.makedirs("data", exist_ok=True)
    with open("data/bulletin.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote data/bulletin.json with %d zone(s)" % len(zones))

    try:
        prev = {}
        try:
            with open("data/bulletin.json", encoding="utf-8") as f:
                prev = json.load(f).get("observations", {}) or {}
        except Exception:
            pass
        print()
        print("station observations (km/h at source, stored as knots)")
        obs = fetch_observations(prev)
        if obs:
            out["observations"] = obs
            with open("data/bulletin.json", "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception as e:
        print("observations failed (bulletin unaffected): %s" % e)
    return 0


# ---------------------------------------------------------------------------
# Station observations — PROBE ONLY, nothing depends on this yet.
#
# The page currently links to Kelp Reefs, Saturna and the rest but never reads
# them, so every figure on it is forecast rather than observation. Before
# building a "what actually happened" comparison, we need to know whether
# these stations publish machine-readable data at all.
#
# The marine list on the datamart turned out to be moored buoys only, and the
# nearest is off Nanaimo. These are lightstations, so they should be in the
# general station list instead. This section looks them up and reports what it
# finds. Every part is wrapped so a failure here can never affect the bulletin.
# ---------------------------------------------------------------------------

STATION_LIST = "https://dd.weather.gc.ca/today/observations/doc/swob-xml_station_list.csv"
SWOB_LATEST = "https://dd.weather.gc.ca/today/observations/swob-ml/latest/"

# Names as they appear on weather.gc.ca, and the leg each one covers.
# Pinned by station code, not by name. Name matching found three candidates
# for Saturna and picked the wrong one: CVTS is an air-quality site inland,
# and "EAST POINT (AUT)" is in Nova Scotia. CWEZ is the East Point lightstation
# this app actually links to. Coordinates below are from the station list and
# match the ones already in LINKS.
STATIONS = {
    "kelp":      {"code": "CWZO", "name": "Kelp Reefs",          "lat": 48.5476, "lon": -123.2369},
    "saturna":   {"code": "CWEZ", "name": "Saturna Island",      "lat": 48.7832, "lon": -123.0458},
    "discovery": {"code": "CWDR", "name": "Discovery Island",    "lat": 48.4246, "lon": -123.2258},
    "racerocks": {"code": "CWQK", "name": "Race Rocks",          "lat": 48.2980, "lon": -123.5314},
    "esquimalt": {"code": "CWPF", "name": "Esquimalt Harbour",   "lat": 48.4320, "lon": -123.4393},
    "yyj":       {"code": "CYYJ", "name": "Victoria Int'l",      "lat": 48.6472, "lon": -123.4260},
}



KMH_TO_KN = 1.0/1.852
OBS_KEEP_H = 36          # enough to cover a full local day either side of UTC midnight


def obs_dirs(day, code):
    return ["https://dd.weather.gc.ca/today/observations/swob-ml/%s/%s/" % (day, code),
            "https://dd.weather.gc.ca/%s/WXO-DD/observations/swob-ml/%s/%s/" % (day, day, code)]


def parse_swob(xml):
    """
    One hourly observation. Speeds are published in KM/H — reading them as
    knots would overstate every figure by a factor of 1.85, so the conversion
    is not optional.
    """
    root = ET.fromstring(xml)
    vals = {}
    for el in root.iter():
        nm = el.get("name")
        if nm and el.get("value") is not None:
            vals[nm] = el.get("value")

    def num(*names):
        for n in names:
            try:
                return float(vals[n])
            except (KeyError, ValueError, TypeError):
                continue
        return None

    # Hourly average is the like-for-like match for an hourly forecast; the
    # 10-minute average stands in when it is missing.
    spd = num("avg_wnd_spd_10m_pst1hr", "avg_wnd_spd_10m_pst10mts", "avg_wnd_spd_10m_pst2mts")
    gst = num("max_wnd_spd_10m_pst1hr", "max_wnd_spd_10m_pst10mts")
    dr  = num("avg_wnd_dir_10m_pst1hr", "avg_wnd_dir_10m_pst10mts", "avg_wnd_dir_10m_pst2mts")
    when = vals.get("date_tm")
    if not when or spd is None:
        return None
    return {"t": when,
            "kn": round(spd*KMH_TO_KN, 1),
            "gust": round(gst*KMH_TO_KN, 1) if gst is not None else None,
            "dir": int(dr) if dr is not None else None}


def fetch_observations(previous):
    """
    Hourly wind for each station, today and yesterday UTC.

    Only files we don't already hold are downloaded, so the first run
    backfills and later ones fetch an hour or two. That keeps this to a
    handful of requests instead of ~180 every hour.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
    cutoff = (now - timedelta(hours=OBS_KEEP_H)).isoformat().replace("+00:00", "Z")

    out = {}
    for key, st in STATIONS.items():
        have = {r["t"]: r for r in (previous.get(key, {}) or {}).get("readings", [])
                if r.get("t", "") >= cutoff}
        added = 0
        for day in days:
            names = []
            for base in obs_dirs(day, st["code"]):
                try:
                    names = [(base, n) for n in re.findall(
                        r'href="([^"]+-swob\.xml)"',
                        listing(base))]
                    break
                except Exception:
                    continue
            for base, n in sorted(names):
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})", n)
                if not m:
                    continue
                stamp = "%s-%s-%sT%s:%s:00.000Z" % m.groups()
                if stamp < cutoff or stamp in have:
                    continue
                try:
                    rec = parse_swob(get(base + n))
                except Exception:
                    continue
                if rec:
                    have[rec["t"]] = rec
                    added += 1
        if have:
            out[key] = {"code": st["code"], "name": st["name"],
                        "lat": st["lat"], "lon": st["lon"],
                        "readings": sorted(have.values(), key=lambda r: r["t"])}
            last = out[key]["readings"][-1]
            print("  %-10s %-18s %d readings (+%d new), latest %s  %.1f kn" % (
                key, st["name"], len(have), added, last["t"][11:16], last["kn"]))
        else:
            print("  %-10s %-18s no readings" % (key, st["name"]))
    return out


if __name__ == "__main__":
    sys.exit(main())
