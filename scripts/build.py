"""
Brown Isle product pipeline.

Reads the Ragic master export (products.csv), makes sure every product photo is
stored in this repository, and builds the SumUp import file.

Inputs (repository root):
  products.csv        Ragic "All PRODUCTS" export - upload a fresh copy whenever products change
  sumup_export.csv    (optional) SumUp catalogue export - lets SumUp update existing items
                      instead of creating duplicates
  image_map.csv       which photo file belongs to which Ragic record (kept up to date by this script)

Outputs:
  <photo files>       new or missing photos downloaded from Ragic
  sumup/sumup_import.csv   file to import into SumUp (Items -> Import)
  sumup/report.txt         summary: new items, updated items, items to delete in SumUp, problems
"""
import csv, html, os, re, sys, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("GITHUB_REPOSITORY", "brown-owl-design/products")
BRANCH = os.environ.get("BRANCH", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
OFFLINE = os.environ.get("OFFLINE") == "1"   # for testing: skip downloads

SUMUP_COLUMNS = ["Item name","Variations","Option set 1","Option 1","Option set 2","Option 2","Option set 3","Option 3","Option set 4","Option 4","Is variation visible? (Yes/No)","Price","Cost price","Variable price? (Yes/No)","Tax rate (%)","On sale in Online Store?","Regular price (before sale)","Set up different prices and VAT for takeaway","Takeaway price","Takeaway tax rate","Unit","Track inventory? (Yes/No)","Quantity","Low stock threshold","SKU","Barcode","Modifiers","Description (Online Store and Invoices only)","Category","Display item at Checkout? (Yes/No)","Display colour in POS checkout","Image 1","Image 2","Image 3","Image 4","Image 5","Image 6","Image 7","Display item in Online Store? (Yes/No)","SEO title (Online Store only)","SEO description (Online Store only)","Shipping weight [kg] (Online Store only)","Display service in Bookings? (Yes/No)","Duration [minutes] (Bookings only)","Location [business/customer] (Bookings only)","Item id (Do not change)","Variant id (Do not change)"]

# Short names for schools longer than SumUp's 30-character category limit
CATEGORY_FIXES = {
    "BALLYCARRICKMADDY PRIMARY SCHOOL": "BALLYCARRICKMADDY PRIMARY",
    "DRUMGOR PRIMARY SCHOOL CRAIGAVON": "DRUMGOR PRIMARY, CRAIGAVON",
    "LISSAN PRIMARY SCHOOL COOKSTOWN": "LISSAN PRIMARY, COOKSTOWN",
    "ST CLAIRES ABBEY PRIAMRY, NEWRY": "ST CLAIRES ABBEY PRIMARY NEWRY",
    "ST. PATRICK'S PRIMARY AGHAGALLON": "ST PATRICK'S PS AGHAGALLON",
    "WARINGSTOWN PRIMARY, WARINGSTOWN": "WARINGSTOWN PRIMARY",
}
TYPO_FIXES = {"PRIAMRY": "PRIMARY"}


def clean(s):
    return " ".join((s or "").replace(chr(0x2028), " ").split())


def fix_typos(s):
    for a, b in TYPO_FIXES.items():
        s = s.replace(a, b)
    return s


def short_category(c):
    c = CATEGORY_FIXES.get(c, c)
    if len(c) <= 30:
        return c
    c = c.replace(" PRIMARY SCHOOL", " PS").replace(" PRIMARY", " PS").replace("SCHOOL", "").strip(" ,")
    while len(c) > 30 and " " in c:
        c = c.rsplit(" ", 1)[0].strip(" ,")
    return c[:30]


def read_csv(name):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def ragic_filename(url):
    """IMG_00931.jpg from https://...file.jsp?a=brownowl&f=FPQ2wea6w4%40IMG_00931.jpg"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    f = q.get("f", [""])[0]
    name = f.split("@", 1)[1] if "@" in f else f
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "image.jpg"
    if not re.search(r"\.(jpe?g|png|gif|webp)$", name, re.I):
        name += ".jpg"
    return name


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "brown-isle-product-sync"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = r.headers.get("Content-Type", "")
                data = r.read()
            if not ctype.startswith("image"):
                raise ValueError(f"not an image ({ctype})")
            with open(dest, "wb") as f:
                f.write(data)
            return None
        except Exception as e:  # noqa: BLE001
            err = str(e)
            time.sleep(2 * (attempt + 1))
    return err


def main():
    master = read_csv("products.csv")
    if not master:
        sys.exit("products.csv not found - upload the Ragic export first.")
    head = [clean(h) for h in master[0]]

    def col(name, nth=0):
        idx = [i for i, h in enumerate(head) if h == name]
        return idx[nth] if len(idx) > nth else None

    C = dict(name=col("Product Name"), school=col("School"), colours=col("Colours"),
             category=col("Product Category"), image=col("Image"), price=col("Price", 1),
             record=col("Title field"))
    if C["price"] is None or not any(r[C["price"]].strip() for r in master[1:] if len(r) > C["price"]):
        C["price"] = col("Price")
    missing_cols = [k for k, v in C.items() if v is None]
    if missing_cols:
        sys.exit(f"products.csv is missing columns: {missing_cols}")

    # image map
    imap = {}
    for r in (read_csv("image_map.csv") or [])[1:]:
        if len(r) >= 3:
            imap[r[0]] = {"filename": r[1], "source_url": r[2]}
    used = {v["filename"]: k for k, v in imap.items()}

    problems, downloaded, products = [], [], []
    for r in master[1:]:
        r = r + [""] * (len(head) - len(r))
        rec = clean(r[C["record"]])
        name, school, colours, cat = (clean(r[C[k]]) for k in ("name", "school", "colours", "category"))
        url = clean(r[C["image"]])
        if not rec or not (name or school or cat or url):
            continue  # empty row
        price = clean(r[C["price"]]).replace(chr(0xA3), "").replace(",", "")

        image_link = ""
        if url:
            entry = imap.get(rec)
            if not entry or entry["source_url"] != url:
                fname = ragic_filename(url)
                if fname in used and used[fname] != rec:
                    fname = f"{rec}_{fname}"
                entry = {"filename": fname, "source_url": url}
                imap[rec] = entry
                used[fname] = rec
            path = os.path.join(ROOT, entry["filename"])
            if not os.path.exists(path):
                err = "skipped (offline test)" if OFFLINE else download(url, path)
                if err:
                    problems.append(f"BI-{int(rec):03d}: photo could not be downloaded ({err})")
                else:
                    downloaded.append(entry["filename"])
            if os.path.exists(path) or OFFLINE:
                image_link = RAW + urllib.parse.quote(entry["filename"])
        else:
            problems.append(f"BI-{int(rec):03d}: no photo in Ragic")

        multi = len(school) > 60
        category = "MULTIPLE SCHOOLS" if multi else short_category(fix_typos(school) or "GENERAL")
        who = (colours + " (multiple schools)") if (multi and colours) else fix_typos(school)
        title = " - ".join(p for p in [cat or "HAIR ACCESSORY", who] if p)
        desc = " ".join(p for p in [
            name + "." if name else "",
            f"Colours: {colours}." if colours else "",
            f"Schools: {school}." if multi else ""] if p)
        products.append({
            "Item name": title,
            "Price": price,
            "Variable price? (Yes/No)": "No" if price else "Yes",
            "Unit": "each.each",
            "Track inventory? (Yes/No)": "No",
            "SKU": f"BI-{int(rec):03d}",
            "Description (Online Store and Invoices only)": desc,
            "Category": category,
            "Display item at Checkout? (Yes/No)": "Yes",
            "Image 1": image_link,
            "Display item in Online Store? (Yes/No)": "Yes",
            "Display service in Bookings? (Yes/No)": "No",
        })

    # Link products to SumUp's existing items so they are updated, not duplicated.
    # SumUp's export does not keep our SKUs, so sumup_ids.csv remembers which SumUp
    # item belongs to which product. Items SumUp created last time are recognised by
    # matching their details against the previous sumup_import.csv.
    ids = {}
    for r in (read_csv("sumup_ids.csv") or [])[1:]:
        if len(r) >= 4:
            ids[r[0]] = {"item": r[1], "variant": r[2], "filename": r[3]}
    export = read_csv("sumup_export.csv")
    to_delete, unknown, sumup_img = [], [], {}
    current = {p["SKU"]: p for p in products}
    if export:
        eh = export[0]
        E = [dict(zip(eh, r + [""] * (len(eh) - len(r)))) for r in export[1:]]
        key = lambda x: tuple(html.unescape(clean(x.get(f, ""))) for f in (
            "Item name", "Description (Online Store and Invoices only)", "Category", "Price"))
        live = {e["Item id (Do not change)"] for e in E}
        ids = {k: v for k, v in ids.items() if v["item"] in live}   # forget items deleted in SumUp
        known = {v["item"] for v in ids.values()}
        by_item = {e["Item id (Do not change)"]: e for e in E}
        for e in E:   # SKU kept by SumUp
            sku = clean(e.get("SKU", ""))
            if sku.startswith("BI-") and e["Item id (Do not change)"] not in known and sku not in ids:
                ids[sku] = {"item": e["Item id (Do not change)"], "variant": e.get("Variant id (Do not change)", ""), "filename": ""}
                known.add(e["Item id (Do not change)"])
        prev_rows = []
        pm = read_csv("sumup/sumup_import.csv") or []
        if pm:
            prev_rows = [dict(zip(pm[0], r)) for r in pm[1:]]
        pool = {}
        for pr in prev_rows:
            if pr.get("SKU") and pr["SKU"] not in ids:
                pool.setdefault(key(pr), []).append(pr)
        for e in E:
            iid = e["Item id (Do not change)"]
            if iid in known:
                continue
            cands = pool.get(key(e))
            if cands:
                pr = cands.pop(0)
                fn = urllib.parse.unquote(pr.get("Image 1", "").rsplit("/", 1)[-1])
                ids[pr["SKU"]] = {"item": iid, "variant": e.get("Variant id (Do not change)", ""), "filename": fn}
                known.add(iid)
            else:
                unknown.append(f"{e['Item name']}  (SumUp item {iid})")
        for sku, v in ids.items():
            if sku not in current and v["item"] in by_item:
                to_delete.append(f"{sku}  {by_item[v['item']]['Item name']}")
            if v["item"] in by_item:
                sumup_img[sku] = by_item[v["item"]].get("Image 1", "")
    for p in products:
        v = ids.get(p["SKU"])
        if v and p["SKU"] in current:
            p["Item id (Do not change)"] = v["item"]
            p["Variant id (Do not change)"] = v["variant"]
            fn = urllib.parse.unquote(p["Image 1"].rsplit("/", 1)[-1]) if p["Image 1"] else ""
            if fn and fn == v["filename"] and sumup_img.get(p["SKU"]):
                p["Image 1"] = sumup_img[p["SKU"]]   # photo unchanged: keep SumUp's copy
            else:
                v["filename"] = fn

    with open(os.path.join(ROOT, "sumup_ids.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "sumup_item_id", "sumup_variant_id", "photo_filename"])
        for sku in sorted(ids):
            w.writerow([sku, ids[sku]["item"], ids[sku]["variant"], ids[sku]["filename"]])

    os.makedirs(os.path.join(ROOT, "sumup"), exist_ok=True)
    with open(os.path.join(ROOT, "sumup", "sumup_import.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMUP_COLUMNS, restval="")
        w.writeheader()
        w.writerows(products)

    with open(os.path.join(ROOT, "image_map.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["record", "filename", "source_url"])
        for rec in sorted(imap, key=lambda x: int(x) if x.isdigit() else 0):
            w.writerow([rec, imap[rec]["filename"], imap[rec]["source_url"]])

    new = sum(1 for p in products if not p.get("Item id (Do not change)"))
    lines = [
        f"Products in master sheet: {len(products)}",
        f"Photos downloaded from Ragic this run: {len(downloaded)}",
        "",
        "SumUp import file: sumup/sumup_import.csv",
    ]
    if export or ids:
        lines += [f"  Will UPDATE existing SumUp items: {len(products) - new}",
                  f"  Will CREATE new SumUp items:      {new}",
                  "", f"In SumUp but no longer in the master sheet ({len(to_delete)}) - delete these by hand in SumUp:"]
        lines += ["  " + x for x in to_delete] or ["  none"]
        if unknown:
            lines += ["", f"In SumUp but not recognised ({len(unknown)}) - check these by hand:"] + ["  " + x for x in unknown]
    else:
        lines += ["  WARNING: no sumup_export.csv uploaded - every row will be created as a NEW item.",
                  "  Export your catalogue from SumUp and upload it here as sumup_export.csv first."]
    lines += ["", f"Problems ({len(problems)}):"] + (["  " + p for p in problems] or ["  none"])
    with open(os.path.join(ROOT, "sumup", "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
