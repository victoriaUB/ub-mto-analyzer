import json, re, sys

def paragraphs(body):
    cut = min([i for i in (body.find("Best regards"), body.find("Thanks and regards")) if i > 0]
              or [len(body)])
    return [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", body[:cut]) if p.strip()]

def records(body, has_brand=True, has_qty=True):
    """Andreina's tables come through as one paragraph per cell. A paragraph
    that is just digits starts a new row; the rest are its fields."""
    out, cur = [], None
    for p in paragraphs(body):
        if re.fullmatch(r"\d{11,14}", p):
            if cur: out.append(cur)
            cur = {"ean": p, "fields": []}
        elif cur is not None:
            cur["fields"].append(p)
    if cur: out.append(cur)
    rows = []
    for r in out:
        f = r["fields"]
        price = next((x for x in reversed(f) if "€" in x), None)
        if not price: continue
        i = f.index(price)
        rest = f[:i]
        if has_qty and rest and re.fullmatch(r"[\d.,]+", rest[-1]):
            rest = rest[:-1]
        brand = rest[0] if (has_brand and rest) else ""
        desc = " — ".join(rest[1:] if has_brand else rest)
        rows.append((r["ean"].zfill(13), brand, desc,
                     price.replace("€", "").strip().replace(".", "").replace(",", ".")))
    return rows
