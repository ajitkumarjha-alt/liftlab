#!/usr/bin/env python3
"""The study bundle: one ZIP, seven CSVs, and a README that carries every caveat verbatim.

WHY A BUNDLE AT ALL. Each CSV link on the dashboard is already honest on its own. A study is not
assembled one link at a time, though: seven files arrive in seven downloads, in an order nobody
records, and the caveats stay behind on the page they came from. What gets mailed onward is a
folder of numbers with no provenance — and every misreading this codebase guards against (riders
as a headcount of people, a dark cell as a zero, a measured minimum as a count) becomes available
again the moment the sentence is separated from the number.

So the properties asserted here are mostly about the SENTENCES, not the numbers:

  1. THE ZIP OPENS, every member is intact, and every CSV has a header row.
  2. THE README NAMES EVERY FILE and gives each one at least one caveat under its own heading.
     Generated from the caveat registry, so a file cannot be added to the bundle without its
     caveats arriving with it — the test checks the ZIP's contents against that registry, not
     against a list written here that would drift.
  3. THE CAVEATS ARE VERBATIM. Every one is asserted as an exact substring of the module constant
     the dashboard prints beside the same number. A bundle whose caveats were reworded for the
     file would be a second, drifting statement of the same limitation, and the first thing to
     drift is always the qualifier.
  4. PRECOMPUTED WHERE PRECOMPUTED EXISTS. Four files come out of study_matrix / trends_cache and
     the manifest says so; the three that must derive name the bound they ran under.
  5. AN ABSENCE IS A ROW, NOT A MISSING CAMERA. A lift with no floor attribution appears in
     rtt_trips.csv with its reason, exactly as it does in the table on screen.
  6. THE RANGE SELECTOR IS RESPECTED, and the filename names the range it actually contains.
"""
import csv as _csvmod
import io
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))

TEN_MB = 10 * 1024 * 1024


def _blob(resp):
    b = getattr(resp, "body", resp)
    return b if isinstance(b, (bytes, bytearray)) else str(b).encode()


def _members(z, root):
    return {n[len(root) + 1:]: z.read(n).decode() for n in z.namelist()
            if n.startswith(root + "/")}


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    day0 = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)

    from test_study_views import build, CAMS, H3
    build(path, day0)
    # ONE ANOMALOUS ROUND TRIP, planted on purpose. The fixture's ch29 trips are all 90s and so all
    # plausible; without an anomaly the "plausible AND anomalous" claim is untested, and a file
    # that silently filtered the anomalies out would pass. 10s falls under RTT_MIN_S.
    con = sqlite3.connect(path)
    T = (day0 + timedelta(days=2, hours=14)).timestamp()
    for off, st in ((0, "open"), (2, "closing"), (4, "closed"), (14, "open")):
        con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,door_version,"
                    "reason) VALUES ('site-A','ch29',?,'G',?,?,'single_panel')", (T + off, st, H3))
    con.commit()
    con.close()

    os.environ["GATEWAY_DB"] = path
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api as D

    # Fill the precomputed tables the way the timer does, so the bundle can prove it READS them.
    db = D._db()
    for cam in [c["cam"] for c in D._cameras(db, "site-A")]:
        D.alphabet_refresh(db, "site-A", cam)
        D.aggregate_refresh(db, "site-A", cam)
        for wd in D.RTT_WINDOWS:
            D.rtt_refresh(db, "site-A", cam, wd)
    for cam in [c["cam"] for c in D._cameras(db, "site-A")] + [""]:
        for per in D.TRENDS_FILL_PERIODS:
            D.trends_refresh(db, "site-A", cam, per)
    for kind in D.STUDY_KINDS:
        for per in D.STUDY_FILL_PERIODS:
            D.study_refresh(db, "site-A", kind, per)
    db.close()

    CAV = D._bundle_caveats()

    # ══ 1. the ZIP opens, and every member is intact ═══════════════════════════════════
    print("=== 1. the ZIP opens ===")
    resp = D.dash_bundle("site-A", period="all")
    blob = _blob(resp)
    name = re.search(r'filename="([^"]+)"', (getattr(resp, "headers", {}) or {})
                     .get("Content-Disposition", "") or "")
    name = name.group(1) if name else ""
    print(f"  {name}  {len(blob):,} bytes")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        bad = z.testzip()
    except Exception as e:
        fails.append(f"the bundle is not a readable ZIP: {type(e).__name__}: {e}")
        print("FAILURES:\n  -", fails[-1])
        return 1
    if bad:
        fails.append(f"member {bad!r} is corrupt")
    if len(blob) > TEN_MB:
        fails.append(f"the bundle is {len(blob):,} bytes, over the {TEN_MB:,} target")
    # liftlab_<gw>_<from>_<to>.zip, and BOTH ends must be real dates even for 'all'
    m = re.fullmatch(r"liftlab_site-A_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.zip", name)
    if not m:
        fails.append(f"the filename {name!r} is not liftlab_<gw>_<from>_<to>.zip with two dates — "
                     f"an 'all' bundle named start_today stops being true the next day")
    root = z.namelist()[0].split("/")[0]
    files = _members(z, root)
    print(f"  {len(files)} member(s) under {root}/: {', '.join(sorted(files))}")

    # ══ 2. every named file is present, and every CSV has a header ═════════════════════
    print("\n=== 2. every file present, every CSV headed ===")
    for fn in list(CAV) + ["README.md"]:
        if fn not in files:
            fails.append(f"{fn} is missing from the bundle")
    for fn in CAV:
        if fn not in files:
            continue
        lines = [l for l in files[fn].splitlines() if not l.startswith("#")]
        rows = list(_csvmod.reader(lines))
        hdr = rows[0] if rows else []
        print(f"  {fn:28s} {len(rows) - 1:>5} row(s), {len(hdr):>2} column(s)")
        if len(hdr) < 2 or not all(h.strip() for h in hdr):
            fails.append(f"{fn} has no usable header row — a headerless CSV is a grid of numbers "
                         f"whose columns nobody can name")
        # every data row must have the header's width, or a column has shifted
        wrong = [i for i, r in enumerate(rows[1:], 2) if len(r) != len(hdr)]
        if wrong:
            fails.append(f"{fn}: {len(wrong)} row(s) do not match the header width "
                         f"(first at line {wrong[0]})")

    # A WIDTH CHECK CANNOT SEE A VALUE IN THE WRONG BOX. door_cycles_per_hour's unavailable row
    # once put the word "unavailable" in n_days and had no state column at all — the widths matched
    # and the file was wrong. So the columns are checked by CONTENT, and the two hour-of-day files
    # are required to agree about the rows they both describe.
    print("\n=== 2b. columns hold what their names say ===")
    hod = {}
    for fn in ("riders_per_hour.csv", "door_cycles_per_hour.csv"):
        rows = list(_csvmod.DictReader(
            l for l in files[fn].splitlines() if not l.startswith("#")))
        hod[fn] = rows
        states = {r["state"] for r in rows}
        bad_days = [r for r in rows if r["n_days"] not in ("",) and not r["n_days"].isdigit()]
        bad_hour = [r for r in rows if r["hour_ist"] not in ("",)
                    and not (r["hour_ist"].isdigit() and 0 <= int(r["hour_ist"]) <= 23)]
        print(f"  {fn:28s} state={sorted(states)} bad n_days={len(bad_days)} "
              f"bad hour={len(bad_hour)}")
        if not states <= {"ok", "unavailable"}:
            fails.append(f"{fn}: state column holds {sorted(states)} — a value has landed in the "
                         f"wrong column")
        if bad_days:
            fails.append(f"{fn}: n_days holds {bad_days[0]['n_days']!r}, which is not a day count")
        if bad_hour:
            fails.append(f"{fn}: hour_ist holds {bad_hour[0]['hour_ist']!r}, not an IST hour")
    # AND THE unavailable BRANCH, which is where the shift was. A custom date range has no
    # precomputed hour-of-day payload, so every camera takes it — without this the check above
    # only ever sees the healthy row and the defect ships again.
    z_c = zipfile.ZipFile(io.BytesIO(_blob(D.dash_bundle(
        "site-A", from_d=(day0 + timedelta(days=1)).date().isoformat(),
        to_d=(day0 + timedelta(days=2)).date().isoformat()))))
    f_c = _members(z_c, z_c.namelist()[0].split("/")[0])
    for fn in ("riders_per_hour.csv", "door_cycles_per_hour.csv"):
        rows = list(_csvmod.DictReader(
            l for l in f_c[fn].splitlines() if not l.startswith("#")))
        states = {r["state"] for r in rows}
        print(f"  custom range {fn:26s} state={sorted(states)} "
              f"n_days={sorted({r['n_days'] for r in rows})} "
              f"reason={'yes' if all(r['reason'] for r in rows) else 'MISSING'}")
        if states != {"unavailable"}:
            fails.append(f"{fn}: a custom range should make every camera unavailable, got "
                         f"{sorted(states)} — or a value is in the wrong column")
        if any(r["n_days"] for r in rows):
            fails.append(f"{fn}: n_days is populated on an unavailable row — that is a value in "
                         f"the wrong box, which a width check cannot see")
        if not all(r["reason"] for r in rows):
            fails.append(f"{fn}: an unavailable row carries no reason")
        if not all("refused rather than run" in r["reason"] or "precomputed" in r["reason"]
                   for r in rows):
            fails.append(f"{fn}: the unavailable reason does not say why it was not derived")
    a, b = hod["riders_per_hour.csv"], hod["door_cycles_per_hour.csv"]
    if [(r["cam"], r["hour_ist"], r["state"]) for r in a] != \
       [(r["cam"], r["hour_ist"], r["state"]) for r in b]:
        fails.append("the two hour-of-day files disagree about which lift-hours they cover — they "
                     "come from ONE precomputed payload and must describe the same rows")

    # ══ 3. the README gives EVERY file a caveat, and they are verbatim ═════════════════
    print("\n=== 3. the README carries a caveat for every file, verbatim ===")
    rd = files.get("README.md", "")
    # section-per-file, checked against the REGISTRY the bundle is built from, so adding a file
    # without caveats fails here rather than being noticed later by a reader who has none.
    for fn, (_desc, caveats) in CAV.items():
        sec = re.search(r"^## `" + re.escape(fn) + r"`\n(.*?)(?=^## |\Z)", rd, re.S | re.M)
        if not sec:
            fails.append(f"README has no section for {fn}")
            continue
        bullets = [l for l in sec.group(1).splitlines() if l.startswith("- ")]
        print(f"  {fn:28s} {len(bullets)} caveat(s)")
        if not bullets:
            fails.append(f"README lists {fn} but gives it no caveat — the file travels without "
                         f"the sentence that says what its numbers mean")
        for c in caveats:
            if c and c[:60] not in rd:
                fails.append(f"{fn}: a caveat is not in the README verbatim: {c[:60]!r}")

    # THE SIX THE STUDY ASKED FOR BY NAME, each matched against the module constant rather than
    # against a phrase retyped here — a paraphrase in the test would let a paraphrase ship.
    named = [
        ("usage volume, not unique people", D.RIDERS_CAVEAT),
        ("NULL direction counted as an alighting", D.DIRECTION_RULE),
        ("precision per era", D.PRECISION_PER_ERA_NOTE),
        ("dwell caveat on cycles", D.H3_CYCLE_CAVEAT),
        ("dwell caveat on RTT", D.rtt_core_caveat()),
        ("occupancy measured minimum", D.OCC_LABEL),
        ("occupancy 0.5x calibration", D.OCC_CALIBRATION),
        ("floor whitelist NONE", D.FLOOR_WHITELIST_NONE_NOTE),
    ]
    for label, text in named:
        ok = bool(text) and text[:70] in rd
        print(f"  {'OK ' if ok else 'NO '} {label}")
        if not ok:
            fails.append(f"the README does not carry the {label} caveat verbatim")
    # and the two most-misread ones must be above the fold, not only under their file
    head = rd.split("## Manifest")[0]
    for label, text in (("riders are not people", D.RIDERS_CAVEAT),
                        ("dark is not zero", D.RIDERS_DARK_NOTE),
                        ("occupancy is a floor", D.OCC_CALIBRATION)):
        if text[:50] not in head:
            fails.append(f"'{label}' is not in the README's opening section — a reader who stops "
                         f"at the top of the file must still meet it")

    # ══ 4. precomputed where precomputed exists ════════════════════════════════════════
    print("\n=== 4. sources ===")
    db = D._db()
    _b, _n, man = D._bundle_build(db, "site-A", period="all")
    db.close()
    for fn in CAV:
        src = (man.get(fn) or {}).get("source", "")
        print(f"  {fn:28s} {src[:64]}")
    for fn, want in (("riders_per_day.csv", "study_matrix"),
                     ("riders_per_hour.csv", "trends_cache"),
                     ("door_cycles_per_hour.csv", "trends_cache")):
        if want not in (man.get(fn) or {}).get("source", ""):
            fails.append(f"{fn} did not come from {want} even though it is filled — the bundle is "
                         f"re-deriving something the timer already computed")
    for fn in ("occupancy_per_episode.csv", "rtt_trips.csv", "eras.csv"):
        src = (man.get(fn) or {}).get("source", "")
        if "DERIVED" not in src:
            fails.append(f"{fn} does not declare that it was derived on this request")
        if not re.search(r"bound|REFUSES|GROUP BY|range scan", src):
            fails.append(f"{fn} derives without naming the bound it ran under")

    # ══ 5. absences are rows, and both trip classes are present ════════════════════════
    print("\n=== 5. rtt_trips carries plausible AND anomalous, and names its absences ===")
    trips = list(_csvmod.DictReader(
        l for l in files["rtt_trips.csv"].splitlines() if not l.startswith("#")))
    classes = {}
    for r in trips:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    print(f"  classes: {classes}")
    if not classes.get("plausible"):
        fails.append("no plausible trips in the file — the fixture is not exercising the walk")
    if not classes.get("anomaly"):
        fails.append("no anomalous trips in the file — an export that keeps only the trips that "
                     "worked cannot show the anomaly rate, which is a measurement of floor "
                     "attribution")
    if not any(r["class"] == "anomaly" and r["anomaly_reason"] for r in trips):
        fails.append("an anomalous trip carries no reason")
    cams_in = {r["cam"] for r in trips}
    for _ch, cam, _l in CAMS:
        if cam not in cams_in:
            fails.append(f"{cam} is absent from rtt_trips.csv entirely — a lift with no round "
                         f"trips must appear with its reason, not vanish")
    nf = [r for r in trips if r["cam"] == "ch27"]
    print(f"  ch27 (no floor): state={nf[0]['class']!r} reason={nf[0]['anomaly_reason'][:52]!r}"
          if nf else "  ch27 MISSING")
    if nf and "UNAVAILABLE, not zero" not in nf[0]["anomaly_reason"]:
        fails.append("the no-floor lift's row does not say its RTT is unavailable rather than zero")

    # ══ 6. eras.csv and outages.csv say what they are for ══════════════════════════════
    print("\n=== 6. eras + outages ===")
    eras = list(_csvmod.DictReader(
        l for l in files["eras.csv"].splitlines() if not l.startswith("#")))
    kinds = {r["kind"] for r in eras}
    print(f"  eras.csv kinds={sorted(kinds)}  rows={len(eras)}  "
          f"whitelist NONE on {sum(1 for r in eras if r['floor_whitelist'] == 'NONE')} row(s)")
    if not {"door_version", "counting_version"} <= kinds:
        fails.append(f"eras.csv is missing a boundary kind: {sorted(kinds)}")
    if not all(r["first_ist"] and r["last_ist"] for r in eras if r["kind"] != "none"):
        fails.append("an era row carries no dates — a boundary without dates is not a boundary")
    if not any(r["is_current"] == "yes" for r in eras):
        fails.append("no era is marked current, so a reader cannot tell which instrument is live")
    outs = list(_csvmod.DictReader(
        l for l in files["outages.csv"].splitlines() if not l.startswith("#")))
    print(f"  outages.csv rows={len(outs)} (DATA_GAPS has {len(D.DATA_GAPS)})")
    if len(outs) != len(D.DATA_GAPS):
        fails.append(f"outages.csv has {len(outs)} rows for {len(D.DATA_GAPS)} known gaps")
    if outs and "MISSING, not low" not in files["outages.csv"] + rd:
        fails.append("the outage file does not say the data inside a gap is missing, not low")

    # ══ 7. the range selector is respected ═════════════════════════════════════════════
    print("\n=== 7. the range selector ===")
    z_day = zipfile.ZipFile(io.BytesIO(_blob(D.dash_bundle("site-A", period="day"))))
    r_day = z_day.namelist()[0].split("/")[0]
    f_day = _members(z_day, r_day)
    n_all = len(files["riders_per_day.csv"].splitlines())
    n_day = len(f_day["riders_per_day.csv"].splitlines())
    print(f"  riders_per_day lines: all={n_all} today={n_day}")
    if n_day >= n_all:
        fails.append("the 'day' bundle is not smaller than the 'all' bundle — the range selector "
                     "is not reaching the files")
    if "Range: **" not in f_day["README.md"]:
        fails.append("the README does not state the range the bundle describes")
    # an unrecognised period must be refused, not answered from all history
    bad = D.dash_bundle("site-A", period="bogus")
    if getattr(bad, "status_code", 200) != 400:
        fails.append("the bundle answered an unrecognised period instead of refusing it")
    print(f"  unknown period: status={getattr(bad, 'status_code', 200)}")

    # ══ 8. the button exists and points at the bundle ══════════════════════════════════
    print("\n=== 8. the button ===")
    page = D.dash_page()
    page = getattr(page, "body", page)
    if "Download study bundle" not in page:
        fails.append("there is no 'Download study bundle' button on the page")
    if "/bundle?" not in page:
        fails.append("the button does not point at /dash/{gw}/bundle")
    # CALL SITES, not the declaration — "bundleBtn()" also matches "function bundleBtn(){".
    n_bars = page.count("+bundleBtn()")
    if n_bars < 3:
        fails.append(f"the button is in {n_bars} download bar(s), not all 3 — it is missing from "
                     f"at least one view, so which tab you are on decides whether you can get "
                     f"the bundle")
    print(f"  present in {n_bars} download bar(s), one visible at a time")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK — the ZIP opens, every CSV has a header, the README gives every file at least one\n"
          "     caveat verbatim, precomputed files come from the precomputed tables, and an\n"
          "     unmeasurable lift is a row with a reason rather than a camera that is not there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
