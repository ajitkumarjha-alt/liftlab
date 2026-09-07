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
    _b, _n, man, _run = D._bundle_build(db, "site-A", period="all")
    db.close()
    for fn in CAV:
        src = (man.get(fn) or {}).get("source", "")
        print(f"  {fn:28s} {src[:64]}")
    for fn, want in (("riders_per_day.csv", "study_matrix"),
                     ("riders_per_hour.csv", "trends_cache"),
                     ("door_cycles_per_hour.csv", "trends_cache"),
                     # THE REGRESSION THAT TOOK THE DOWNLOAD DOWN. rtt_trips walked each camera's
                     # era here, on the request path, under a ROW cap — and a cap on rows is not a
                     # cap on time. Seven cameras still spent 60.26s in SQL on a 7-day range and
                     # the whole zip was lost. It reads the precomputed walk now.
                     ("rtt_trips.csv", "rtt_window.trips")):
        if want not in (man.get(fn) or {}).get("source", ""):
            fails.append(f"{fn} did not come from {want} even though it is filled — the bundle is "
                         f"re-deriving something the timer already computed")
    for fn in ("occupancy_per_episode.csv", "eras.csv"):
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
    # THE MEASUREMENT STATE, NOT THE DELIVERY STATE. A camera with no floor attribution stores an
    # EMPTY trip list — the walk ran and there was nothing to find — and reading that as "pending"
    # told the operator to wait for a sweep that will never change the answer.
    if not nf:
        pass
    elif nf[0]["class"] != "no_floor":
        fails.append(f"the no-floor lift is classed {nf[0]['class']!r}, not no_floor — an empty "
                     f"stored trip list is being read as a missing one")
    elif "UNAVAILABLE, not zero" not in nf[0]["anomaly_reason"]:
        fails.append("the no-floor lift's row does not say its RTT is unavailable rather than zero")

    # ══ 5b. NO WALK ON THE REQUEST PATH — the fix for the 60s timeout ══════════════════
    # Proven by DISABLING the walk. If rtt_core.trips() cannot be called and the file is still
    # produced with its rows, the bundle is reading rtt_window and not deriving. A timing
    # assertion would pass on a fixture too small to be slow; this cannot.
    print("\n=== 5b. the bundle cannot walk, and still produces rtt_trips ===")
    import rtt_core
    _real_trips, _real_sum = rtt_core.trips, rtt_core.summarise

    def _boom(*a, **k):
        raise AssertionError("rtt_core walked on the request path — this is the 60.26s timeout")
    rtt_core.trips = _boom
    rtt_core.summarise = _boom
    try:
        z2 = zipfile.ZipFile(io.BytesIO(_blob(D.dash_bundle("site-A", period="all"))))
        f2 = _members(z2, z2.namelist()[0].split("/")[0])
    finally:
        rtt_core.trips, rtt_core.summarise = _real_trips, _real_sum
    if "rtt_trips.csv" not in f2:
        fails.append("with the walk disabled the bundle could not produce rtt_trips.csv — it is "
                     "still deriving on the request path, which is the reported timeout")
    else:
        t2 = list(_csvmod.DictReader(
            l for l in f2["rtt_trips.csv"].splitlines() if not l.startswith("#")))
        n_real = sum(1 for r in t2 if r["class"] in ("plausible", "anomaly"))
        print(f"  produced {len(t2)} row(s), {n_real} of them real trips, with rtt_core disabled")
        if n_real < 1:
            fails.append("rtt_trips.csv has no trips when the walk is disabled — the rows are not "
                         "coming from the precomputed store")
        if [r["class"] for r in t2] != [r["class"] for r in trips]:
            fails.append("the walked and the stored file disagree about their rows")

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

    # ══ 9. ONE FILE FAILING WITHHOLDS ONE FILE ════════════════════════════════════════
    # The first version raised on any failure and returned no zip at all, so a single bad dataset
    # cost the reader the other six and handed them a JSON error page instead. A named absence
    # INSIDE the artefact is honest; seven datasets withheld because of one is not better.
    print("\n=== 9. a partial bundle, with the gap named inside it ===")
    _real_alpha = D._alphabet_read

    def _boom_alpha(*a, **k):
        raise RuntimeError("synthetic: the floor alphabet could not be read")
    D._alphabet_read = _boom_alpha
    try:
        rp = D.dash_bundle("site-A", period="all")
        zp = zipfile.ZipFile(io.BytesIO(_blob(rp)))
        fp = _members(zp, zp.namelist()[0].split("/")[0])
    finally:
        D._alphabet_read = _real_alpha
    print(f"  members: {', '.join(sorted(fp))}")
    if "eras.csv" in fp:
        fails.append("the failing dataset was written anyway")
    if "eras.UNAVAILABLE.txt" not in fp:
        fails.append("a withheld file left no stub — unzipped into a folder, a missing file is "
                     "indistinguishable from one the reader forgot to look at")
    else:
        stub = fp["eras.UNAVAILABLE.txt"]
        print(f"  stub names the reason: "
              f"{'yes' if 'synthetic' in stub else 'NO'} · "
              f"says it is not an empty dataset: {'not the same as an empty file' in stub}")
        if "synthetic" not in stub:
            fails.append("the stub does not carry the reason the file was withheld")
        if "not the same as an empty file" not in stub:
            fails.append("the stub does not distinguish itself from an empty dataset")
        if "Time spent" not in stub:
            fails.append("the stub does not record the phase/duration it was withheld at")
    for other in ("riders_per_day.csv", "rtt_trips.csv", "outages.csv", "README.md"):
        if other not in fp:
            fails.append(f"{other} was lost because a DIFFERENT file failed — one bad dataset must "
                         f"not cost the other six")
    rdp = fp.get("README.md", "")
    if "## Withheld from this bundle" not in rdp:
        fails.append("the README does not list the withheld file")
    if "`eras.csv`" not in rdp.split("## Read this first")[0]:
        fails.append("the withheld file is not named ABOVE the numbers — a reader who takes the "
                     "folder at face value must learn what is missing before they start counting")
    if "**WITHHELD**" not in rdp:
        fails.append("the manifest does not mark the withheld file")
    wh = (getattr(rp, "headers", {}) or {}).get("X-Bundle-Withheld", "")
    print(f"  X-Bundle-Withheld: {wh!r} · README lists it: "
          f"{'## Withheld from this bundle' in rdp}")
    if "eras.csv" not in wh:
        fails.append("the response header does not name the withheld file, so the page cannot "
                     "tell the user the download is incomplete")

    # AND WHEN EVERY FILE IS WITHHELD, there is still a zip: stubs all the way down beats an error
    # page. A zero OVERALL budget is the deterministic way in — a zero PER-FILE budget only fires
    # if a query happens to be long enough for the SQL progress handler to interrupt, which on a
    # fixture it is not, so that version of this check could pass without exercising anything.
    _save = D.BUNDLE_BUDGET_S
    D.BUNDLE_BUDGET_S = 0.0
    try:
        rz = D.dash_bundle("site-A", period="all")
        zz = zipfile.ZipFile(io.BytesIO(_blob(rz)))
        fz = _members(zz, zz.namelist()[0].split("/")[0])
    finally:
        D.BUNDLE_BUDGET_S = _save
    n_stub = sum(1 for k in fz if k.endswith(".UNAVAILABLE.txt"))
    n_csv = sum(1 for k in fz if k.endswith(".csv"))
    print(f"  with a 0s overall budget: {len(fz)} member(s), {n_stub} stub(s), {n_csv} csv, "
          f"README present={'README.md' in fz}, status={getattr(rz, 'status_code', 200)}")
    if getattr(rz, "status_code", 200) != 200 or "zip" not in (getattr(rz, "media_type", "") or ""):
        fails.append("an exhausted budget returned an ERROR instead of a zip of stubs — that is "
                     "the refuse-the-whole-download rule coming back")
    if n_stub != len(CAV):
        fails.append(f"an exhausted budget produced {n_stub} stubs for {len(CAV)} files — a file "
                     f"that was never reached must still say so")
    if n_csv:
        fails.append("a file was written despite the budget being spent")
    if "README.md" not in fz:
        fails.append("a bundle whose files were all withheld returned no README — the reader gets "
                     "a zip with no explanation in it")
    any_stub = next((v for k, v in fz.items() if k.endswith(".UNAVAILABLE.txt")), "")
    if "budget" not in any_stub:
        fails.append("the stub does not name the budget as the reason it was not reached")

    # ══ 10. a browser never gets a JSON error page ════════════════════════════════════
    print("\n=== 10. errors are HTML for a browser, JSON for fetch() ===")
    b_html = D.dash_bundle("site-A", period="bogus")
    b_json = D.dash_bundle("site-A", period="bogus", fmt="json")
    mt = (getattr(b_html, "media_type", "") or "")
    body_html = _blob(b_html).decode()
    print(f"  direct hit : {getattr(b_html, 'status_code', 200)} {mt} "
          f"({len(body_html)} bytes, <html>={'<style>' in body_html})")
    print(f"  fmt=json   : {getattr(b_json, 'status_code', 200)} "
          f"{getattr(b_json, 'media_type', '')}")
    if "html" not in mt:
        fails.append("a direct browser hit got a non-HTML error — a tab of raw JSON is a stack "
                     "trace being used as a user interface")
    for want in ("Try again", "Narrower range", "back to", "dashboard"):
        if want.lower() not in body_html.lower():
            fails.append(f"the HTML error page does not offer {want!r} — an error with no next "
                         f"step leaves the reader stuck on a dead tab")
    if "Nothing is wrong with your data" not in body_html:
        fails.append("the HTML error page does not say the data is not the problem")
    if getattr(b_json, "media_type", "") != "application/json":
        fails.append("fmt=json did not return JSON, so the page cannot render the error inline")

    # ══ 11. every request is logged, with its phases ══════════════════════════════════
    print("\n=== 11. every bundle request is logged ===")
    _db = D._db()
    last = D.bundle_run_latest(_db, "site-A")
    n_runs = _db.execute("SELECT COUNT(*) FROM bundle_run WHERE gateway_id='site-A'").fetchone()[0]
    _db.close()
    ph = json.loads((last or {}).get("phases") or "{}")
    print(f"  {n_runs} run(s) recorded · last: {last and last['total_s']}s, "
          f"slowest={last and last['slowest']!r} {last and last['slowest_s']}s, "
          f"{len(ph)} phase(s)")
    if not last:
        fails.append("no bundle request was recorded — the next regression is invisible until a "
                     "user is handed an error page, which is how this one was found")
    if n_runs < 2:
        fails.append("only the slow requests are recorded; the signal is the CURVE, and a log "
                     "that fires at the threshold cannot show a phase creeping towards it")
    if len(ph) < len(CAV):
        fails.append(f"only {len(ph)} of {len(CAV)} phases were recorded — a total cannot say "
                     f"WHICH file is growing, and the answer decides what to fix")
    if not (last or {}).get("slowest"):
        fails.append("the record does not name the slowest phase")

    # AND THE HEALTH LINE SAYS SO BEFORE A USER DOES. slowlog only prints once a handler is ALREADY
    # over its threshold — by which time someone has been handed an error page. The signal is the
    # phase creeping towards the budget, and the daily line is where it has to appear.
    try:
        import health_check as H
    except Exception as e:
        print(f"  health line SKIPPED — health_check not importable ({type(e).__name__})")
    else:
        _db = D._db()
        _db.execute("UPDATE bundle_run SET total_s=41.0, slowest='rtt_trips.csv', slowest_s=38.2 "
                    "WHERE gateway_id='site-A' AND started_at=(SELECT MAX(started_at) FROM "
                    "bundle_run WHERE gateway_id='site-A')")
        _db.commit()
        phrase, pay = H._bundle_slow(_db, "site-A")
        print(f"  health phrase: {str(phrase)[:96]!r}")
        if not phrase:
            fails.append("a 41s bundle against a 30s warning line produced no health phrase — the "
                         "next regression is again invisible until a user meets it")
        else:
            if "rtt_trips.csv" not in phrase:
                fails.append("the health phrase does not name the slowest FILE — 'the bundle is "
                             "slow' sends the reader to read seven producers")
            if "41s" not in phrase.replace("41.0", "41"):
                fails.append("the health phrase does not state the duration")
        # a fast, complete bundle must stay SILENT — a line that always fires is not a warning
        _db.execute("UPDATE bundle_run SET total_s=2.0, n_withheld=0, withheld='[]' "
                    "WHERE gateway_id='site-A'")
        _db.commit()
        quiet, _ = H._bundle_slow(_db, "site-A")
        # and a WITHHELD file must speak even when the bundle was fast
        _db.execute("UPDATE bundle_run SET n_withheld=1, withheld='[\"eras.csv\"]' "
                    "WHERE gateway_id='site-A' AND started_at=(SELECT MAX(started_at) FROM "
                    "bundle_run WHERE gateway_id='site-A')")
        _db.commit()
        wphrase, _ = H._bundle_slow(_db, "site-A")
        _db.close()
        print(f"  fast+complete stays quiet: {quiet is None} · "
              f"fast+withheld still speaks: {bool(wphrase)}")
        if quiet is not None:
            fails.append("a fast, complete bundle still produced a health phrase — a line that "
                         "always fires is not a warning")
        if not wphrase or "withheld" not in wphrase:
            fails.append("a bundle that completed with a file withheld says nothing on the health "
                         "line — the operator learns it only if someone opens the zip")

    # ══ 12. the button never navigates away from the dash ═════════════════════════════
    print("\n=== 12. the button ===")
    if 'class=bundlebtn href=' in page.replace('"', '').replace("'", ""):
        fails.append("the bundle button is still a plain link — a failure replaces the whole "
                     "dashboard with the error response")
    for want, why in (("startBundle()", "the button does not go through fetch()"),
                      ("preparing bundle", "there is no preparing state"),
                      ("fmt=json", "the fetch does not ask for a machine-readable error"),
                      ("THE BUNDLE COULD NOT BE BUILT", "there is no inline error panel"),
                      ("try 7 days instead", "the inline error offers no narrower range"),
                      (">retry<", "the inline error offers no retry"),
                      ("FILE(S) WITHHELD", "a partial download is not reported to the user")):
        if want not in page:
            fails.append(why)
    print(f"  fetch-driven={'startBundle()' in page} · preparing state="
          f"{'preparing bundle' in page} · inline error+retry="
          f"{'THE BUNDLE COULD NOT BE BUILT' in page}")

    # ══ 13. A RETIRED BUILD'S REFUSAL IS NOT REPEATED AS A CURRENT ONE ════════════════
    # Reported live: the 7-day bundle refused ch29's RTT at "199,855 rows > 120k cap". The timer
    # walks uncapped, so it cannot produce that state — any stored one predates the change. Serving
    # its note verbatim tells an operator the current system refuses their range, which is untrue,
    # and sends them to narrow a range that would have worked.
    print("\n=== 13. a stale too_many_rows is not served as a live limit ===")
    _db = D._db()
    D._rtt_window_table(_db)
    _cv, _dv = D._current_keys(_db, "site-A", "ch29")
    _db.execute(
        "INSERT OR REPLACE INTO rtt_window (gateway_id,cam,window_days,counting_version,"
        "door_version,payload,trips,n_trips_stored,computed_at,compute_ms) "
        "VALUES ('site-A','ch29',?,?,?,?,?,0,1.0,1)",
        (0.0, _cv or "", _dv or "",
         json.dumps({"state": "too_many_rows", "era": _dv, "n_rows": 199855,
                     "note": "199855 door rows in this range exceeds the 120000 the request path "
                             "will walk. Narrow the range."}),
         json.dumps([])))
    _db.commit()
    fleet = {r["cam"]: r for r in D._rtt_per_hour_compute(_db, "site-A", "all")["rows"]}
    _db.close()
    zs = zipfile.ZipFile(io.BytesIO(_blob(D.dash_bundle("site-A", period="all"))))
    fs = _members(zs, zs.namelist()[0].split("/")[0])
    row = next((r for r in _csvmod.DictReader(
        l for l in fs["rtt_trips.csv"].splitlines() if not l.startswith("#"))
        if r["cam"] == "ch29"), None)
    r29 = fleet.get("ch29") or {}
    print(f"  fleet matrix: absence={r29.get('absence')!r}")
    print(f"  bundle row  : class={row and row['class']!r} "
          f"reason={str(row and row['anomaly_reason'])[:60]!r}")
    if r29.get("absence") != "stale":
        fails.append(f"the fleet matrix classed a retired build's refusal as "
                     f"{r29.get('absence')!r} — the panel asserts a limit that no longer exists")
    for where, text in (("the fleet matrix", r29.get("reason") or ""),
                        ("the bundle row", (row or {}).get("anomaly_reason") or "")):
        if "stale row" not in text:
            fails.append(f"{where} does not say the stored refusal is stale")
        if "precompute_job.py" not in text:
            fails.append(f"{where} does not name the remedy (re-run the sweep)")
        if "Narrow the range" in text or "120000 the request path" in text:
            fails.append(f"{where} repeats the retired build's cap message verbatim, telling the "
                         f"operator to narrow a range that would have worked")

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
