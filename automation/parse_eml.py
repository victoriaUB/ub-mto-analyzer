#!/usr/bin/env python3
"""Parse forwarded MTO offer emails (.eml files, or a .zip of them) into one
analyzer input CSV.

Andreina's offers arrive as an HTML table in the email body, sometimes with an
xlsx/csv attachment that holds more rows. This reads both: every .eml given is
parsed, the body table and any spreadsheet attachment are merged, and the brand
falls back to the subject line when the table has no Brand column.

Usage:
    python3 automation/parse_eml.py ~/Downloads/*.eml            # -> offers.csv
    python3 automation/parse_eml.py ~/Downloads/mail.zip -o out.csv
    python3 automation/parse_eml.py x.eml --per-offer            # one CSV each

Then analyze:
    KEEPA_API_KEY=... python3 automation/mto_pipeline.py --dry-run offers.csv
"""

import argparse
import email
import email.policy
import io
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402

SHEET_EXTS = (".xlsx", ".xls", ".csv")
# "DELVERY" is Andreina's recurring typo — without it the lead time is dropped
TERM_KEYWORDS = ["MOQ", "MOA", "DELIVERY", "DELVERY", "MADE TO ORDER",
                 "SUBJECT TO UNSOLD", "EXPIR", "PAYMENT"]
# Complete phrases — emit as-is instead of slicing forward into the product table
TERM_PHRASES = {"MADE TO ORDER", "SUBJECT TO UNSOLD"}


def brand_from_subject(subject):
    """'Fwd: AZZARO NEWS*' -> 'AZZARO'. Strips forward prefixes and noise words."""
    s = re.sub(r"^\s*(re|fwd|fw)\s*:\s*", "", subject or "", flags=re.I).strip()
    s = re.sub(r"\*+", "", s)
    s = re.split(r"\s*[-–|]\s*", s)[0]
    s = re.sub(r"\b(news|offer|oferta|new|nuevo|stock|promo|mto|top seller|"
               r"made to order|price list|pricelist)\b", "", s, flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" .:,-")


def offer_terms(text):
    """Offer conditions, one per keyword. Each term runs from its keyword to the
    next one, so 'MOQ: 12 PCS/REF. DELIVERY: 6-8 WEEKS' yields two clean terms
    instead of one long overlapping blob."""
    flat = re.sub(r"\s+", " ", text or "")
    hits = sorted((m.start(), kw) for kw in TERM_KEYWORDS
                  for m in re.finditer(re.escape(kw), flat, flags=re.I))
    terms = []
    for i, (pos, kw) in enumerate(hits):
        if kw in TERM_PHRASES:
            seg = kw
        else:
            end = hits[i + 1][0] if i + 1 < len(hits) else min(len(flat), pos + 45)
            seg = flat[pos:end]
            seg = re.split(r"[.;|]\s|\s{3,}", seg)[0]          # stop at sentence end
            seg = re.sub(r"\s{2,}", " ", seg).strip(" .:*-·|,")[:45]
        if seg and not any(seg.upper() in t.upper() for t in terms):
            terms = [t for t in terms if t.upper() not in seg.upper()] + [seg]
    return terms


EAN_LINE = re.compile(r"^EAN\s*:?\s*(\d[\d\s-]{6,16})$", re.I)
PRICE_LINE = re.compile(r"PRICE\s*:?\s*\**\s*([\d]+[.,]?\d*)\s*(?:€|EUR)", re.I)
QTY_LINE = re.compile(r"^QTY\b", re.I)
SKIP_LINE = re.compile(r"^(MOQ|MOA|DELIVERY|DELVERY|MADE TO ORDER|SUBJECT TO|"
                       r"EXPIR|PAYMENT|BEST REGARDS|PRE-ORDER|EMAIL|TLF|FAX|"
                       r"WEBSITE|DIRECCI|ANTES DE|CUIDEMOS|FROM|TO|CC|DATE|"
                       r"SUBJECT|-{3,}|\[image)", re.I)


def items_from_text(text, brand=""):
    """Fallback for offers written as Word-style paragraphs instead of a table:

        *AZZARO SPORT EDT VAPO 100 ML (new pack)*
        EAN 3614273667418
        QTY: 150 PCS
        *PRICE: 12.80€*

    Each EAN line anchors one product: the title is the nearest preceding
    descriptive line, the price the next PRICE line after it.
    """
    lines = [re.sub(r"\*+", "", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    items, seen = [], set()
    for i, ln in enumerate(lines):
        m = EAN_LINE.match(ln)
        if not m:
            continue
        ean = core.normalize_ean(m.group(1))
        if not ean or ean in seen:
            continue
        price = None
        for nxt in lines[i + 1:i + 5]:
            pm = PRICE_LINE.search(nxt)
            if pm:
                price = float(pm.group(1).replace(",", "."))
                break
        if price is None or price <= 0:
            continue
        title = ""
        for prev in reversed(lines[:i]):
            if (EAN_LINE.match(prev) or QTY_LINE.match(prev) or SKIP_LINE.match(prev)
                    or PRICE_LINE.search(prev) or not re.search(r"[A-Za-z]{3}", prev)):
                continue
            title = prev
            break
        seen.add(ean)
        items.append({"ean": ean, "title": title, "price_eur": price, "brand": brand})
    return items


def _walk(msg):
    """Yield (content_type, filename, payload) for every leaf part."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        yield part.get_content_type(), (part.get_filename() or ""), part


def items_from_eml(path_or_bytes, name=""):
    """Parse one .eml -> (items, subject, brand, terms, sources)."""
    if isinstance(path_or_bytes, bytes):
        msg = email.message_from_bytes(path_or_bytes, policy=email.policy.default)
    else:
        with open(path_or_bytes, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)
        name = name or os.path.basename(path_or_bytes)

    subject = msg.get("subject", "") or name
    brand = brand_from_subject(subject)
    items, sources, text_parts = [], [], []
    seen = set()

    def add(new_items, src):
        added = 0
        for it in new_items:
            if it["ean"] in seen:
                continue
            seen.add(it["ean"])
            if not it.get("brand"):
                it["brand"] = brand
            items.append(it)
            added += 1
        if added:
            sources.append(f"{src}: {added}")

    # Spreadsheet attachments first — they usually hold the complete offer.
    for ctype, fname, part in _walk(msg):
        if fname.lower().endswith(SHEET_EXTS):
            data = part.get_payload(decode=True) or b""
            try:
                if fname.lower().endswith(".csv"):
                    got, _, _ = core.items_from_dataframe(pd.read_csv(io.BytesIO(data)), brand)
                else:
                    got, _, _ = core.items_from_excel(data, brand)
                add(got, f"attachment {fname}")
            except Exception as e:
                sources.append(f"attachment {fname}: unreadable ({e})")

    # Then the body table(s).
    for ctype, fname, part in _walk(msg):
        if ctype == "text/html":
            html = part.get_content()
            text_parts.append(re.sub(r"<[^>]+>", " ", html))
            try:
                tables = pd.read_html(io.StringIO(html))
            except ValueError:
                tables = []
            for t in sorted(tables, key=lambda x: -len(x)):
                if len(t) < 2:
                    continue
                try:
                    got, _, _ = core.items_from_dataframe(t, brand)
                except ValueError:
                    continue
                if got:
                    add(got, "email body")
                    break
        elif ctype == "text/plain":
            text_parts.append(part.get_content())

    if not items:
        got = items_from_text("\n".join(text_parts), brand)
        add(got, "email body (paragraph format)")

    return items, subject, brand, offer_terms("\n".join(text_parts)), sources


def collect_paths(paths):
    """Expand .zip archives into (name, bytes); pass .eml files through."""
    out = []
    for p in paths:
        if p.lower().endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.lower().endswith(".eml"):
                        out.append((os.path.basename(n), z.read(n)))
        elif p.lower().endswith(".eml"):
            out.append((os.path.basename(p), p))
        else:
            print(f"  skipping {p} (not .eml or .zip)", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help=".eml files, or a .zip containing them")
    ap.add_argument("-o", "--out", default="offers.csv")
    ap.add_argument("--per-offer", action="store_true",
                    help="write one CSV per email instead of a combined file")
    args = ap.parse_args()

    all_rows, summary = [], []
    for name, src in collect_paths(args.paths):
        items, subject, brand, terms, sources = items_from_eml(src, name)
        print(f"\n{name}")
        print(f"  subject : {subject}")
        print(f"  brand   : {brand or '(none — will use Keepa)'}")
        print(f"  products: {len(items)}   [{'; '.join(sources) or 'none found'}]")
        if terms:
            print(f"  terms   : {' · '.join(terms)}")
        if not items:
            continue
        rows = [{"Brand": it["brand"], "Product": it["title"], "EAN": it["ean"],
                 "Purchase price EUR": it["price_eur"], "Offer": name,
                 "Terms": " · ".join(terms)} for it in items]
        if args.per_offer:
            out = os.path.splitext(name)[0] + ".csv"
            pd.DataFrame(rows).to_csv(out, index=False)
            print(f"  -> {out}")
        all_rows += rows
        summary.append((name, brand, len(items), " · ".join(terms)))

    if not all_rows:
        print("\nNo products found in any file.", file=sys.stderr)
        sys.exit(1)

    # Same EAN in two offers: keep the cheaper one, and say so.
    df = pd.DataFrame(all_rows)
    dupes = df[df.duplicated(subset=["EAN"], keep=False)]
    df = (df.sort_values("Purchase price EUR")
            .drop_duplicates(subset=["EAN"], keep="first")
            .sort_index())
    if len(dupes):
        n = dupes["EAN"].nunique()
        print(f"\n{n} EAN(s) appeared in more than one offer — kept the lowest price:")
        for ean, grp in dupes.groupby("EAN"):
            prices = ", ".join(f"{r['Offer']} {r['Purchase price EUR']:.2f}"
                               for _, r in grp.iterrows())
            if grp["Purchase price EUR"].nunique() > 1:
                print(f"  {ean}: {prices}")
    df.to_csv(args.out, index=False)
    print(f"\n{'='*60}\n{len(df)} unique products from {len(summary)} offer(s) -> {args.out}")
    for name, brand, n, terms in summary:
        print(f"  {brand or '?':<22} {n:>4} products   {name}")


if __name__ == "__main__":
    main()
