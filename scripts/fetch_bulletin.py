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

BASE = "https://dd.weather.gc.ca/today/marine_weather/pacific"
UA = {"User-Agent": "boatcheck/1.0 (+https://github.com/) marine bulletin fetcher"}
TIMEOUT = 30

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


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def hour_dirs():
    """Newest hour folder first. Directory listing is plain Apache HTML."""
    html = get(BASE + "/").decode("utf-8", "replace")
    hours = sorted(set(re.findall(r'href="(\d{2})/"', html)), reverse=True)
    if not hours:
        raise RuntimeError("no hour folders found at " + BASE)
    return hours


def find_file(hours, code):
    """Walk back from the newest hour until this site code turns up."""
    for hh in hours:
        try:
            html = get("%s/%s/" % (BASE, hh)).decode("utf-8", "replace")
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


def issued_iso(root):
    dt = root.find("./lastModifiedTime/dateTime")
    if dt is None:
        return None
    ts = text_of(dt.find("timeStamp"))
    if len(ts) == 12 and ts.isdigit():
        try:
            return datetime.strptime(ts, "%Y%m%d%H%M").replace(
                tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def build_zone(key, spec, hours):
    url = find_file(hours, spec["code"])
    if not url:
        raise RuntimeError("no current file for %s (%s)" % (key, spec["code"]))
    root = ET.fromstring(get(url))

    reg = pick_location(root.find("regularForecast"), spec["match"])
    ext = pick_location(root.find("extendedForecast"), spec["match"])
    wav = pick_location(root.find("waveForecast"), spec["match"])

    forecast = regular_text(reg)
    if not forecast:
        raise RuntimeError("empty forecast text for " + key)

    z = {
        "label": spec["label"],
        "area": (reg.get("name") if reg is not None else "") or spec["label"],
        "issued": issued_iso(root),
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
    hours = hour_dirs()
    print("hour folders available:", ", ".join(hours))

    zones, failed = {}, {}
    for key, spec in ZONES.items():
        try:
            zones[key] = build_zone(key, spec, hours)
            w = len(zones[key]["warnings"])
            print("  %-5s ok  — %s%s" % (
                key, zones[key]["area"],
                (", %d alert(s)" % w) if w else ", no alerts"))
        except Exception as e:
            failed[key] = str(e)
            print("  %-5s FAILED — %s" % (key, e), file=sys.stderr)

    if not zones:
        print("every zone failed; leaving the previous file untouched", file=sys.stderr)
        return 1

    out = {
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "zones": zones,
    }
    if failed:
        out["failed"] = failed

    os.makedirs("data", exist_ok=True)
    with open("data/bulletin.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote data/bulletin.json with %d zone(s)" % len(zones))
    return 0


if __name__ == "__main__":
    sys.exit(main())
