#!/usr/bin/env python3
"""
refresh_prices.py  —  Load real AWS prices into a local database (no credentials).

Downloads AWS's PUBLIC price files for all supported services/regions and writes
them to prices.db. Run this on a schedule (see com.aws-cost-agent.refresh.plist)
so the agent always reads fresh prices instantly.

    pip install ijson      (or: uv run --with ijson python refresh_prices.py)
"""
import os
import sqlite3
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ijson

BASE = "https://pricing.us-east-1.amazonaws.com"
DB = Path(__file__).parent / "prices.db"
DOWNLOAD_TIMEOUT = 60   # seconds of socket inactivity before a read fails (no more infinite hangs)
DOWNLOAD_RETRIES = 3    # transient network failures are common on the big price files

# --- coverage (edit to taste) ------------------------------------------------
REGIONS = ["us-east-1"]      # us-east-1 only
REGIONAL_METERED = {"Lambda": "AWSLambda", "ALB": "AWSELB", "WAF": "awswaf"}
GLOBAL_METERED = {"Route53": "AmazonRoute53", "CloudFront": "AmazonCloudFront"}
# -----------------------------------------------------------------------------


def offer_url(code, region=None):
    if region:
        return f"{BASE}/offers/v1.0/aws/{code}/current/{region}/index.json"
    return f"{BASE}/offers/v1.0/aws/{code}/current/index.json"


def download(url):
    last = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r, open(path, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    f.write(chunk)
            return path
        except Exception as e:
            Path(path).unlink(missing_ok=True)
            last = e
            if attempt < DOWNLOAD_RETRIES:
                print(f"  ! download attempt {attempt}/{DOWNLOAD_RETRIES} failed ({e}); retrying...")
                time.sleep(5 * attempt)
    raise last


def pub_date(path):
    with open(path, "rb") as f:
        return next(ijson.items(f, "publicationDate"), None)


def ondemand_first(path, skus):
    """First On-Demand price per sku -> {sku: (price, unit, desc)}."""
    out = {}
    with open(path, "rb") as f:
        for sku, tb in ijson.kvitems(f, "terms.OnDemand"):
            if sku not in skus:
                continue
            for term in tb.values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd is not None:
                        out[sku] = (float(usd), dim.get("unit", ""), dim.get("description", ""))
                        break
                if sku in out:
                    break
    return out


def parse_instances(path, family, want_attr):
    """Collect SKUs whose productFamily==family; want_attr(a)->dict|None."""
    keep = {}
    with open(path, "rb") as f:
        for sku, p in ijson.kvitems(f, "products"):
            if p.get("productFamily") != family:
                continue
            row = want_attr(p.get("attributes", {}))
            if row is not None:
                keep[sku] = row
    prices = ondemand_first(path, set(keep))
    out = []
    for sku, row in keep.items():
        if sku in prices:
            price, unit, desc = prices[sku]
            out.append((*row, unit, price, desc))
    return out


def parse_dimensions(path):
    """Every On-Demand price dimension with its attrs (for metered services)."""
    attrs = {}
    with open(path, "rb") as f:
        for sku, p in ijson.kvitems(f, "products"):
            a = dict(p.get("attributes", {}))
            a["_family"] = p.get("productFamily", "")
            attrs[sku] = a
    rows = []
    with open(path, "rb") as f:
        for sku, tb in ijson.kvitems(f, "terms.OnDemand"):
            a = attrs.get(sku, {})
            for term in tb.values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd is None:
                        continue
                    rows.append((
                        float(usd), dim.get("unit", ""), dim.get("description", ""),
                        a.get("usagetype", ""), a.get("group", ""), a.get("_family", ""),
                        str(dim.get("beginRange", "0")),
                    ))
    return rows


def init(conn):
    c = conn.cursor()
    for t in ("instances", "storage", "dimensions", "meta"):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.execute("""CREATE TABLE instances(service,region,instance_type,os,presw,license,
                 engine,deployment,tenancy,capacity,unit,price_usd,description,as_of)""")
    c.execute("""CREATE TABLE storage(service,region,item_key,unit,price_usd,description,as_of)""")
    c.execute("""CREATE TABLE dimensions(service_code,region,unit,price_usd,description,
                 usagetype,grp,family,begin_range,as_of)""")
    c.execute("""CREATE TABLE meta(key,value)""")
    c.execute("CREATE INDEX i_inst ON instances(service,region,instance_type)")
    c.execute("CREATE INDEX i_stor ON storage(service,region,item_key)")
    c.execute("CREATE INDEX i_dim ON dimensions(service_code,region)")
    conn.commit()


def refresh():
    # Build into a temp DB and swap it into place only after a fully successful
    # run. A failed/interrupted refresh therefore never wipes the live prices.db.
    fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=str(DB.parent))
    os.close(fd)
    tmp_db = Path(tmp_path)
    try:
        _build(tmp_db)
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        raise
    os.replace(tmp_db, DB)   # atomic on the same filesystem


def _build(db_path):
    conn = sqlite3.connect(db_path)
    init(conn)
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    for region in REGIONS:
        print(f"\n== {region} ==")

        # EC2 compute (all OS/license, Shared+Used) + EBS storage — one file
        f = download(offer_url("AmazonEC2", region))
        as_of = pub_date(f)
        try:
            def ec2(a):
                if a.get("tenancy") == "Shared" and a.get("capacitystatus") == "Used":
                    return ("EC2", region, a.get("instanceType"), a.get("operatingSystem"),
                            a.get("preInstalledSw"), a.get("licenseModel"), None, None,
                            "Shared", "Used")
                return None
            ec2_rows = parse_instances(f, "Compute Instance", ec2)
            for r in ec2_rows:
                c.execute("INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*r, as_of))
            # EBS storage lives in the EC2 file
            ebs_keep = {}
            with open(f, "rb") as fh:
                for sku, p in ijson.kvitems(fh, "products"):
                    a = p.get("attributes", {})
                    if p.get("productFamily") == "Storage" and a.get("volumeApiName"):
                        ebs_keep[sku] = a["volumeApiName"]
            ep = ondemand_first(f, set(ebs_keep))
            for sku, vt in ebs_keep.items():
                if sku in ep:
                    price, unit, desc = ep[sku]
                    c.execute("INSERT INTO storage VALUES (?,?,?,?,?,?,?)",
                              ("EBS", region, vt, unit, price, desc, as_of))
            c.execute("INSERT INTO meta VALUES (?,?)", (f"pub:AmazonEC2:{region}", as_of))
            print(f"  EC2 {len(ec2_rows)} rows, EBS {len(ebs_keep)}")
        finally:
            Path(f).unlink(missing_ok=True)

        # RDS
        f = download(offer_url("AmazonRDS", region))
        as_of = pub_date(f)
        try:
            def rds(a):
                if a.get("deploymentOption") in ("Single-AZ", "Multi-AZ"):
                    return ("RDS", region, a.get("instanceType"), None, None,
                            a.get("licenseModel"), a.get("databaseEngine"),
                            a.get("deploymentOption"), None, None)
                return None
            rds_rows = parse_instances(f, "Database Instance", rds)
            for r in rds_rows:
                c.execute("INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*r, as_of))
            c.execute("INSERT INTO meta VALUES (?,?)", (f"pub:AmazonRDS:{region}", as_of))
            print(f"  RDS {len(rds_rows)} rows")
        finally:
            Path(f).unlink(missing_ok=True)

        # S3
        f = download(offer_url("AmazonS3", region))
        as_of = pub_date(f)
        try:
            s3_keep = {}
            with open(f, "rb") as fh:
                for sku, p in ijson.kvitems(fh, "products"):
                    a = p.get("attributes", {})
                    if p.get("productFamily") == "Storage" and a.get("storageClass"):
                        s3_keep.setdefault(a["storageClass"], sku)
            sp = ondemand_first(f, set(s3_keep.values()))
            n = 0
            for cls, sku in s3_keep.items():
                if sku in sp:
                    price, unit, desc = sp[sku]
                    c.execute("INSERT INTO storage VALUES (?,?,?,?,?,?,?)",
                              ("S3", region, cls, unit, price, desc, as_of))
                    n += 1
            c.execute("INSERT INTO meta VALUES (?,?)", (f"pub:AmazonS3:{region}", as_of))
            print(f"  S3 {n} storage classes")
        finally:
            Path(f).unlink(missing_ok=True)

        # regional metered: Lambda, ALB, WAF
        for label, code in REGIONAL_METERED.items():
            try:
                f = download(offer_url(code, region))
            except Exception as e:
                print(f"  ! {label} {region}: {e}")
                continue
            as_of = pub_date(f)
            try:
                dims = parse_dimensions(f)
                for row in dims:
                    c.execute("INSERT INTO dimensions VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (code, region, row[1], row[0], row[2], row[3], row[4], row[5], row[6], as_of))
                c.execute("INSERT INTO meta VALUES (?,?)", (f"pub:{code}:{region}", as_of))
                print(f"  {label} {len(dims)} dimensions")
            finally:
                Path(f).unlink(missing_ok=True)

    # global metered: Route53, CloudFront (single file each)
    for label, code in GLOBAL_METERED.items():
        try:
            f = download(offer_url(code))
        except Exception as e:
            print(f"  ! {label} global: {e}")
            continue
        as_of = pub_date(f)
        try:
            dims = parse_dimensions(f)
            for row in dims:
                c.execute("INSERT INTO dimensions VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (code, "global", row[1], row[0], row[2], row[3], row[4], row[5], row[6], as_of))
            c.execute("INSERT INTO meta VALUES (?,?)", (f"pub:{code}:global", as_of))
            print(f"\n  {label} {len(dims)} dimensions (global)")
        finally:
            Path(f).unlink(missing_ok=True)

    c.execute("INSERT INTO meta VALUES (?,?)", ("refreshed_at", now))
    conn.commit()
    ni = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    ns = conn.execute("SELECT COUNT(*) FROM storage").fetchone()[0]
    nd = conn.execute("SELECT COUNT(*) FROM dimensions").fetchone()[0]
    conn.close()
    print(f"\nDone. instances={ni} storage={ns} dimensions={nd}. DB: {DB}")


if __name__ == "__main__":
    refresh()
