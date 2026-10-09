"""
cba_scraper.py - pull marching band recap PDFs from coloradomarching.org and
turn them into a tidy table of scores (one row per band per performance).

Used by update.py. Can also be run on its own:
    python cba_scraper.py --season 2026
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PDF_CACHE = DATA / "pdf_cache"
MANIFEST = DATA / "pdf_manifest.csv"
SCRAPED = DATA / "scraped_scores.csv"
CLASSES = DATA / "school_classes.csv"
ALIASES = DATA / "school_aliases.csv"

HEADERS = {"User-Agent": "Mozilla/5.0 (score tracker for personal use)"}

SCRAPED_FIELDS = [
    "season", "event", "event_date", "round", "school", "class", "class_source",
    "total", "penalty", "section", "source_label", "pdf_url", "scraped_at",
]
MANIFEST_FIELDS = ["pdf_url", "season", "section", "label", "status", "rows", "event", "event_date", "checked_at"]

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)
ROUND_RE = re.compile(r"^(prelims?|finals?|semi-?\s?finals?|quarter-?\s?finals?)$", re.I)
CLASS_HDR_RE = re.compile(r"^(?:class\s+([1-5]A)|(open)\s+class)$", re.I)
NUM_TOKEN_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
CLASS_TOKEN_RE = re.compile(r"^[1-5]A$")
# penalty, negative penalty, final total  e.g. "0.1 -0.10 57.400" or "0 0.00 75.800"
TOTAL_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s+(-\d+\.\d{2}|0\.00)\s+(\d{2,3}\.\d{3})(?![\d.])")
THREE_DEC_RE = re.compile(r"(?<![\d.])(\d{2,3}\.\d{3})(?![\d.])")
HEADER_WORDS = {
    "mus", "tot", "ach", "cmp", "rep", "prf", "pen", "total", "sub", "penalties", "timing",
    "individual", "ensemble", "visual", "music", "effect", "general", "performance", "judge",
    "head", "class", "print", "back", "top", "prelims", "finals", "key", "scores", "recap",
    "verified", "missing", "highlights", "t-a-t", "ch-cnt", "&",
}


# ---------------------------------------------------------------- helpers
def load_aliases() -> dict[str, str]:
    out = {}
    if ALIASES.exists():
        with ALIASES.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["alias"].strip().lower()] = r["school"].strip()
    return out


def clean_name(name: str, aliases: dict[str, str]) -> str:
    name = re.sub(r"\s+", " ", name.replace("’", "'")).strip(" ,*")
    name = re.sub(r"\bHigh School$", "HS", name)
    return aliases.get(name.lower(), name)


def load_class_table() -> dict[str, dict]:
    out = {}
    if CLASSES.exists():
        with CLASSES.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["school"].strip()] = r
    return out


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def get(url: str, tries: int = 3) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


# ---------------------------------------------------------------- step 1: find PDF links
def list_pdf_links(page_url: str, season: int) -> list[dict]:
    """Every recap PDF on a scores page, tagged with the heading it sits under."""
    soup = BeautifulSoup(get(page_url).text, "html.parser")
    links, seen, section = [], set(), ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):
        if el.name != "a":
            t = el.get_text(" ", strip=True)
            if t and re.search(r"sanctioned|regional|state|show", t, re.I):
                section = t
            continue
        href = el.get("href") or ""
        if "_files/ugd" not in href or ".pdf" not in href.lower():
            continue
        url = urljoin(page_url, href).split("?")[0]
        if url in seen:
            continue
        seen.add(url)
        links.append({"season": season, "section": section,
                      "label": el.get_text(" ", strip=True), "pdf_url": url})
    return links


# ---------------------------------------------------------------- step 2: parse one PDF
def pdf_lines(pdf_bytes: bytes) -> list[str]:
    lines = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines.extend(l.strip() for l in txt.splitlines() if l.strip())
    return lines


def is_header_text(line: str) -> bool:
    toks = line.lower().split()
    if any(t in HEADER_WORDS for t in toks):
        return True
    if re.search(r"\b[A-Z]\.\s?[A-Z][a-z]", line):  # judge names like "C. Stansberry"
        return True
    return False


def split_name_line(line: str) -> tuple[str, str] | None:
    """'Standley Lake HS 5.3 5.4 ...' -> ('Standley Lake HS', '5.3 5.4 ...')"""
    toks = line.split()
    for i, t in enumerate(toks):
        if NUM_TOKEN_RE.match(t):
            name, rest = toks[:i], toks[i:]
            if not name or not re.search(r"[A-Za-z]", " ".join(name)):
                return None
            if not all(NUM_TOKEN_RE.match(x) or CLASS_TOKEN_RE.match(x) for x in rest):
                return None
            joined = " ".join(name)
            if is_header_text(joined) or DATE_RE.search(line):
                return None
            return joined, " ".join(rest)
    return None


def parse_block(text: str) -> dict | None:
    m = list(TOTAL_RE.finditer(text))
    if m:
        last = m[-1]
        total, penalty, tail = float(last.group(3)), abs(float(last.group(2))), text[last.end():]
    else:
        threes = list(THREE_DEC_RE.finditer(text))
        if not threes:
            return None
        last = threes[-1]
        total, penalty, tail = float(last.group(1)), None, text[last.end():]
    if not 15 <= total <= 100:
        return None
    cls = re.findall(r"\b([1-5]A)\b", tail) or re.findall(r"\b([1-5]A)\b", text)
    return {"total": total, "penalty": penalty, "row_class": cls[-1] if cls else ""}


def parse_recap(pdf_bytes: bytes, aliases: dict[str, str]) -> tuple[dict, list[dict]]:
    lines = pdf_lines(pdf_bytes)
    meta = {"event": "", "location": "", "event_date": "", "round": ""}
    for i, l in enumerate(lines):
        m = DATE_RE.search(l)
        if m:
            meta["event_date"] = dt.datetime.strptime(
                f"{m.group(2)} {m.group(3)} {m.group(4)}", "%B %d %Y").date().isoformat()
            if i >= 1:
                meta["location"] = lines[i - 1]
            if i >= 2:
                meta["event"] = lines[i - 2]
            if i + 1 < len(lines) and ROUND_RE.match(lines[i + 1]):
                meta["round"] = lines[i + 1]
            break

    rows, cur_class, block, name, pending = [], "", [], None, ""

    def close():
        nonlocal block, name
        if name:
            parsed = parse_block(" ".join(block))
            if parsed:
                parsed["school"] = clean_name(name, aliases)
                parsed["section_class"] = cur_class
                rows.append(parsed)
        block, name = [], None

    for l in lines:
        hdr = CLASS_HDR_RE.match(l)
        if hdr:
            close()
            cur_class = (hdr.group(1) or "Open").upper().replace("OPEN", "Open")
            pending = ""
            continue
        split = split_name_line(l)
        if split:
            close()
            nm, rest = split
            if pending and len(pending.split()) <= 4:
                nm = f"{pending} {nm}"
            name, block, pending = nm, [rest], ""
            continue
        toks = l.split()
        if name and all(NUM_TOKEN_RE.match(t) or CLASS_TOKEN_RE.match(t) for t in toks):
            block.append(l)
            continue
        close()
        pending = "" if (is_header_text(l) or DATE_RE.search(l) or re.search(r"\d", l)) else l
    close()
    return meta, rows


def normalize_round(meta_round: str, event: str, label: str) -> str:
    text = f"{event} {label}".lower()
    r = meta_round.lower()
    if "state" in text:
        if "quarter" in text or "quarter" in r:
            return "State Quarterfinals"
        if "semi" in text or "semi" in r:
            return "State Semifinals"
        return "State Finals"
    if "regional" in text:
        return "Regional"
    if "final" in r or re.search(r"\bfinals?\b", label.lower()):
        return "Finals"
    return "Prelims"


# ---------------------------------------------------------------- step 3: run
def scrape_season(season: int, page_url: str, force: bool = False, verbose: bool = True) -> list[dict]:
    """Download and parse any recap PDFs not already processed. Returns new rows."""
    aliases = load_aliases()
    class_table = load_class_table()
    manifest = {r["pdf_url"]: r for r in read_csv(MANIFEST)}
    existing = read_csv(SCRAPED)
    have = {(r["event_date"], r["round"], r["school"]) for r in existing}
    PDF_CACHE.mkdir(parents=True, exist_ok=True)

    links = list_pdf_links(page_url, season)
    if verbose:
        print(f"[{season}] {len(links)} recap PDFs listed on {page_url}")
    new_rows, now = [], dt.datetime.now().isoformat(timespec="seconds")

    for link in links:
        url = link["pdf_url"]
        if not force and manifest.get(url, {}).get("status") in {"parsed", "no_text", "no_rows"}:
            continue
        cache = PDF_CACHE / (hashlib.md5(url.encode()).hexdigest() + ".pdf")
        try:
            if not cache.exists():
                cache.write_bytes(get(url).content)
                time.sleep(1)  # be polite
            meta, rows = parse_recap(cache.read_bytes(), aliases)
        except Exception as e:  # noqa: BLE001
            manifest[url] = {**link, "status": f"error: {e}"[:200], "rows": 0, "checked_at": now}
            print(f"  ! {link['label']}: {e}", file=sys.stderr)
            continue

        status = "parsed" if rows else ("no_rows" if meta["event_date"] else "no_text")
        rnd = normalize_round(meta["round"], meta["event"], link["label"])
        added = 0
        for r in rows:
            cls, src = r["row_class"], "recap row"
            if not cls and r["section_class"] not in ("", "Open"):
                cls, src = r["section_class"], "recap section"
            if not cls and r["school"] in class_table:
                cls, src = class_table[r["school"]]["class"], "lookup table"
            if not cls:
                cls, src = "Unknown", ""
            key = (meta["event_date"], rnd, r["school"])
            if key in have:
                continue  # same performance already captured from another PDF
            have.add(key)
            new_rows.append({
                "season": season, "event": meta["event"] or link["label"],
                "event_date": meta["event_date"], "round": rnd, "school": r["school"],
                "class": cls, "class_source": src, "total": f"{r['total']:.3f}",
                "penalty": "" if r["penalty"] is None else f"{r['penalty']:.2f}",
                "section": link["section"], "source_label": link["label"],
                "pdf_url": url, "scraped_at": now,
            })
            added += 1
            # learn classes from recaps that state them
            if src.startswith("recap") and r["school"] not in class_table:
                class_table[r["school"]] = {"school": r["school"], "class": cls, "region": "", "note": "learned from recap; not on CBA classification list"}
        manifest[url] = {**link, "status": status, "rows": added, "event": meta["event"],
                         "event_date": meta["event_date"], "checked_at": now}
        if verbose:
            flag = "" if rows else f"  <-- {status}: add these scores to data/manual_scores.csv"
            print(f"  {link['label'][:40]:40s} {meta['event_date'] or '?':10s} {rnd:17s} {added:3d} rows{flag}")

    write_csv(SCRAPED, existing + new_rows, SCRAPED_FIELDS)
    write_csv(MANIFEST, list(manifest.values()), MANIFEST_FIELDS)
    write_csv(CLASSES, list(class_table.values()),
              ["school", "class", "region", "enrollment", "enrollment_class", "official_name", "note"])
    unknown = sorted({r["school"] for r in new_rows if r["class"] == "Unknown"})
    if unknown:
        print("  Schools with unknown class (add them to data/school_classes.csv):", ", ".join(unknown))
    return new_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, nargs="*", help="season(s) to scrape; default = config season")
    ap.add_argument("--force", action="store_true", help="re-parse PDFs already processed")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    for s in a.season or [cfg["season"]]:
        scrape_season(s, cfg["score_pages"][str(s)], force=a.force)


if __name__ == "__main__":
    main()
