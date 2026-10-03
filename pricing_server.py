#!/usr/bin/env python3
"""
pricing_server.py  —  Fast AWS pricing MCP server (reads a local price DB).

Reads prices.db (built by refresh_prices.py from AWS's PUBLIC price files) and
answers exact, validated On-Demand pricing questions instantly. No credentials,
no per-query downloads. Refresh the DB on a schedule to stay current.

    pip install fastmcp    (or let uv fetch it, see .mcp.json)
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

DB_PATH = Path(__file__).parent / "prices.db"
HOURS_PER_MONTH = 730
HOURS_PER_YEAR = 8760
STALE_DAYS = 14

VALID_OS = {"Linux", "RHEL", "SUSE", "Windows"}
VALID_LICENSE = {"No License required", "Bring your own license", "NA"}
VALID_PRESW = {"NA", "SQL Std", "SQL Ent", "SQL Web"}

mcp = FastMCP("aws-pricing-live")


# ------------------------------- db access ----------------------------------

def _db():
    if not DB_PATH.exists():
        raise FileNotFoundError("prices.db not found. Run: python refresh_prices.py")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _freshness(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key='refreshed_at'").fetchone()
    if not row:
        return {"refreshed_at": None, "days_old": None, "stale": True}
    try:
        ts = datetime.fromisoformat(row["value"])
        days = (datetime.now(timezone.utc) - ts).days
        return {"refreshed_at": row["value"], "days_old": days, "stale": days > STALE_DAYS}
    except Exception:
        return {"refreshed_at": row["value"], "days_old": None, "stale": False}


def _hourly(price: float) -> dict:
    return {"hourly_usd": round(price, 6),
            "monthly_usd": round(price * HOURS_PER_MONTH, 2),
            "yearly_usd": round(price * HOURS_PER_YEAR, 2)}


def _dims(conn, service_code: str, region: str):
    rows = conn.execute(
        "SELECT price_usd, unit, description, usagetype, grp, family, begin_range "
        "FROM dimensions WHERE service_code=? AND region=?", (service_code, region)
    ).fetchall()
    return [{"price": r["price_usd"], "unit": r["unit"], "description": r["description"],
             "usagetype": r["usagetype"], "group": r["grp"], "family": r["family"],
             "beginRange": r["begin_range"]} for r in rows]


def _txt(d): return f"{d['description']} {d['usagetype']} {d['group']} {d['family']}".lower()


def _pick(dims, all_of=(), any_of=(), none_of=(), unit_has=None, base_tier=True, nonzero=False):
    out = []
    for d in dims:
        if nonzero and not (d.get("price") or 0) > 0:
            continue  # skip $0 lines (free tier, promos) when we want a real rate
        t = _txt(d)
        if not all(s.lower() in t for s in all_of):
            continue
        if any_of and not any(s.lower() in t for s in any_of):
            continue
        if any(s.lower() in t for s in none_of):
            continue
        if unit_has and unit_has.lower() not in d["unit"].lower():
            continue
        out.append(d)
    if not out:
        return None
    if base_tier:
        zero = [d for d in out if str(d.get("beginRange")) in ("0", "0.0")]
        if zero:
            return zero[0]
    return out[0]


# ============================== fixed-size tools =============================

@mcp.tool()
def list_valid_options() -> dict:
    """Accepted values for operating_system, license_model, preinstalled_sw, and
    the DB freshness."""
    conn = _db()
    try:
        return {"operating_system": sorted(VALID_OS), "license_model": sorted(VALID_LICENSE),
                "preinstalled_sw": sorted(VALID_PRESW), "data": _freshness(conn),
                "notes": "On-Demand, Shared tenancy, Used capacity only. Reserved, "
                         "Savings Plans, Dedicated, and capacity reservations excluded. "
                         "BYOL only if requested explicitly."}
    finally:
        conn.close()


@mcp.tool()
def get_ec2_price(instance_type: str, region: str, operating_system: str = "Linux",
                  license_model: str = "No License required", preinstalled_sw: str = "NA") -> dict:
    """Exact, validated On-Demand EC2 price (hourly/monthly/yearly).
    operating_system: Linux|RHEL|SUSE|Windows. license_model: No License required|
    Bring your own license|NA. preinstalled_sw: NA|SQL Std|SQL Ent|SQL Web."""
    if operating_system not in VALID_OS:
        return {"found": False, "error": f"Invalid operating_system '{operating_system}'.", "valid": sorted(VALID_OS)}
    if license_model not in VALID_LICENSE:
        return {"found": False, "error": f"Invalid license_model '{license_model}'.", "valid": sorted(VALID_LICENSE)}
    if preinstalled_sw not in VALID_PRESW:
        return {"found": False, "error": f"Invalid preinstalled_sw '{preinstalled_sw}'.", "valid": sorted(VALID_PRESW)}
    conn = _db()
    try:
        row = conn.execute(
            "SELECT price_usd, description, os, presw, license, tenancy, capacity, as_of "
            "FROM instances WHERE service='EC2' AND region=? AND instance_type=? AND os=? "
            "AND presw=? AND license=?",
            (region, instance_type, operating_system, preinstalled_sw, license_model)).fetchone()
        if not row:
            types = [r["instance_type"] for r in conn.execute(
                "SELECT DISTINCT instance_type FROM instances WHERE service='EC2' AND region=?", (region,))]
            return {"found": False,
                    "message": f"No exact On-Demand match for {instance_type}/{operating_system}"
                               f"/{license_model}/{preinstalled_sw} in {region}.",
                    "available_instance_types": sorted(types)}
        # validate
        checks = {"os": (row["os"], operating_system), "license": (row["license"], license_model),
                  "presw": (row["presw"], preinstalled_sw), "tenancy": (row["tenancy"], "Shared"),
                  "capacity": (row["capacity"], "Used")}
        mism = {k: g for k, (g, e) in checks.items() if g != e}
        if mism:
            return {"found": False, "error": "Validation failed: attribute mismatch.", "mismatches": mism}
        desc = (row["description"] or "").lower()
        if "on demand" not in desc or instance_type.lower() not in desc:
            return {"found": False, "error": "Validation failed: description did not confirm On-Demand.",
                    "description": row["description"]}
        return {"found": True, "service": "EC2", "instance_type": instance_type,
                "operating_system": operating_system, "license_model": license_model,
                "preinstalled_sw": preinstalled_sw, "tenancy": "Shared", "capacity": "Used",
                "pricing_model": "On-Demand", "region": region, "currency": "USD",
                **_hourly(row["price_usd"]), "price_description": row["description"],
                "priced_as_of": row["as_of"], "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_rds_price(instance_type: str, region: str, engine: str = "MySQL",
                  deployment: str = "Single-AZ", license_model: str = "No license required") -> dict:
    """Exact, validated On-Demand RDS price. instance_type e.g. 'db.t3.micro';
    engine e.g. 'MySQL','PostgreSQL'; deployment 'Single-AZ'|'Multi-AZ'."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT price_usd, description, as_of FROM instances WHERE service='RDS' AND region=? "
            "AND instance_type=? AND engine=? AND deployment=? AND license=?",
            (region, instance_type, engine, deployment, license_model)).fetchone()
        if not row:
            ex = [f"{r['instance_type']}|{r['engine']}|{r['deployment']}|{r['license']}"
                  for r in conn.execute("SELECT DISTINCT instance_type,engine,deployment,license "
                                        "FROM instances WHERE service='RDS' AND region=?", (region,))][:40]
            return {"found": False, "message": f"No exact On-Demand RDS match in {region}.",
                    "available_examples": sorted(ex)}
        return {"found": True, "service": "RDS", "instance_type": instance_type, "engine": engine,
                "deployment": deployment, "license_model": license_model, "pricing_model": "On-Demand",
                "region": region, "currency": "USD", **_hourly(row["price_usd"]),
                "price_description": row["description"], "priced_as_of": row["as_of"],
                "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_ebs_price(volume_type: str, region: str, size_gb: float = 100) -> dict:
    """EBS storage cost for a volume type (e.g. 'gp3','gp2') and size."""
    conn = _db()
    try:
        row = conn.execute("SELECT price_usd, description, as_of FROM storage "
                           "WHERE service='EBS' AND region=? AND item_key=?", (region, volume_type)).fetchone()
        if not row:
            av = [r["item_key"] for r in conn.execute("SELECT item_key FROM storage WHERE service='EBS' AND region=?", (region,))]
            return {"found": False, "message": f"No EBS price for '{volume_type}' in {region}.", "available": sorted(av)}
        monthly = row["price_usd"] * size_gb
        return {"found": True, "service": "EBS", "volume_type": volume_type, "region": region,
                "size_gb": size_gb, "price_per_gb_month_usd": round(row["price_usd"], 6),
                "monthly_usd": round(monthly, 2), "yearly_usd": round(monthly * 12, 2), "currency": "USD",
                "price_description": row["description"], "priced_as_of": row["as_of"], "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_s3_price(region: str, size_gb: float = 100, storage_class: str = "General Purpose") -> dict:
    """S3 storage cost. storage_class e.g. 'General Purpose' (Standard)."""
    conn = _db()
    try:
        row = conn.execute("SELECT price_usd, description, as_of FROM storage "
                           "WHERE service='S3' AND region=? AND item_key=?", (region, storage_class)).fetchone()
        if not row:
            av = [r["item_key"] for r in conn.execute("SELECT item_key FROM storage WHERE service='S3' AND region=?", (region,))]
            return {"found": False, "message": f"No S3 price for '{storage_class}' in {region}.", "available": sorted(av)}
        monthly = row["price_usd"] * size_gb
        return {"found": True, "service": "S3", "storage_class": storage_class, "region": region,
                "size_gb": size_gb, "price_per_gb_month_usd": round(row["price_usd"], 6),
                "monthly_usd": round(monthly, 2), "yearly_usd": round(monthly * 12, 2), "currency": "USD",
                "price_description": row["description"], "priced_as_of": row["as_of"], "data": _freshness(conn)}
    finally:
        conn.close()


# ============================ usage-metered tools ============================

@mcp.tool()
def inspect_service_prices(service_code: str, region: str = "ap-south-1", contains: str = "") -> dict:
    """DIAGNOSTIC: raw On-Demand price dimensions for a service. service_code e.g.
    'AWSLambda','AWSELB','awswaf','AmazonRoute53','AmazonCloudFront' (last two use
    region 'global'). Filter with `contains`."""
    conn = _db()
    try:
        dims = _dims(conn, service_code, region)
        c = contains.lower()
        rows = [{"price": d["price"], "unit": d["unit"], "description": d["description"],
                 "usagetype": d["usagetype"], "group": d["group"]} for d in dims if c in _txt(d)]
        return {"service_code": service_code, "region": region, "count": len(rows),
                "dimensions": rows[:60], "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_lambda_price(region: str, monthly_requests: float, memory_mb: float = 128,
                     avg_duration_ms: float = 200, architecture: str = "x86") -> dict:
    """On-Demand Lambda monthly cost. architecture 'x86'|'arm'. Excludes free tier."""
    conn = _db()
    try:
        dims = _dims(conn, "AWSLambda", region)
        if not dims:
            return {"found": False, "error": f"No Lambda prices for {region}."}
        arm = architecture.lower() in ("arm", "arm64", "graviton")
        # Pick the STANDARD on-demand compute + request rates only. Exclude the
        # $0 free-tier lines (nonzero=True) and non-standard variants
        # (provisioned, edge, snapstart, ephemeral storage, managed instances).
        # Standard compute uses unit "Lambda-GB-Second"; storage/web/microvm use
        # "GB-Seconds", so unit_has filters those out.
        if arm:
            req = _pick(dims, all_of=("aws-lambda-requests-arm",), nonzero=True)
            dur = _pick(dims, all_of=("aws-lambda-duration-arm",), none_of=("provisioned",),
                        unit_has="lambda-gb-second", nonzero=True)
        else:
            req = _pick(dims, all_of=("aws-lambda-requests",), none_of=("arm",), nonzero=True)
            dur = _pick(dims, all_of=("aws-lambda-duration",), none_of=("arm", "provisioned"),
                        unit_has="lambda-gb-second", nonzero=True)
        none_arm = () if arm else ("arm",)
        if not req:  # fallback: any paid request line
            req = _pick(dims, all_of=("request",), none_of=none_arm, nonzero=True)
        if not dur:  # fallback: any paid standard-compute GB-second line
            dur = _pick(dims, any_of=("gb-second", "gb second"),
                        none_of=none_arm + ("provisioned", "edge", "snapstart", "storage", "micro", "web"),
                        unit_has="lambda-gb-second", nonzero=True)
        if not req or not dur:
            return {"found": False, "error": "Could not locate Lambda rates.",
                    "hint": "inspect_service_prices('AWSLambda', region)"}
        gb = memory_mb / 1024.0
        gb_seconds = monthly_requests * (avg_duration_ms / 1000.0) * gb
        monthly = monthly_requests * req["price"] + gb_seconds * dur["price"]
        return {"found": True, "service": "Lambda", "region": region, "architecture": architecture,
                "assumptions": {"monthly_requests": monthly_requests, "memory_mb": memory_mb,
                                "avg_duration_ms": avg_duration_ms, "free_tier": "excluded"},
                "request_price_per_unit": req["price"], "duration_price_per_gb_second": dur["price"],
                "gb_seconds": round(gb_seconds, 2), "monthly_usd": round(monthly, 2),
                "yearly_usd": round(monthly * 12, 2), "currency": "USD", "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_alb_price(region: str, avg_lcus: float = 1, hours: float = 730) -> dict:
    """On-Demand Application Load Balancer monthly cost = hourly LB + LCU-hours."""
    conn = _db()
    try:
        dims = _dims(conn, "AWSELB", region)
        lb = _pick(dims, all_of=("application",), any_of=("load balancer", "loadbalancer"),
                   none_of=("lcu",), unit_has="Hrs")
        lcu = _pick(dims, all_of=("lcu",), any_of=("application",)) or _pick(dims, all_of=("lcu",))
        if not lb or not lcu:
            return {"found": False, "error": "Could not locate ALB rates.",
                    "hint": "inspect_service_prices('AWSELB', region)"}
        monthly = hours * lb["price"] + hours * avg_lcus * lcu["price"]
        return {"found": True, "service": "ALB", "region": region,
                "assumptions": {"avg_lcus": avg_lcus, "hours": hours}, "lb_hourly_usd": lb["price"],
                "lcu_hourly_usd": lcu["price"], "monthly_usd": round(monthly, 2),
                "yearly_usd": round(monthly * 12, 2), "currency": "USD", "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_waf_price(region: str, web_acls: int = 1, rules: int = 5, monthly_requests: float = 1_000_000) -> dict:
    """On-Demand AWS WAF monthly cost = Web ACLs + rules + requests."""
    conn = _db()
    try:
        dims = _dims(conn, "awswaf", region)
        acl = _pick(dims, any_of=("web acl", "webacl"), none_of=("shield",), nonzero=True)
        rule = _pick(dims, all_of=("rule",), none_of=("group", "request", "shield"), nonzero=True)
        # Standard per-request rate only — exclude Shield, Bot Control, Fraud
        # Control, Anti-DDoS, Challenge, WCU-tier and size-variant request lines.
        reqd = _pick(dims, all_of=("request",),
                     none_of=("shield", "amr", "bot", "fraud", "challenge", "ddos",
                              "wcu", "kb", "targeted", "capped", "count"),
                     nonzero=True)
        if not acl or not rule or not reqd:
            return {"found": False, "error": "Could not locate WAF rates.",
                    "hint": "inspect_service_prices('awswaf', region)"}
        # reqd["price"] is PER REQUEST (e.g. 6e-7 = $0.60 per million), so multiply
        # by the request count directly — do NOT divide requests by a million.
        monthly = web_acls * acl["price"] + rules * rule["price"] + monthly_requests * reqd["price"]
        return {"found": True, "service": "WAF", "region": region,
                "assumptions": {"web_acls": web_acls, "rules": rules, "monthly_requests": monthly_requests},
                "web_acl_monthly_usd": acl["price"], "rule_monthly_usd": rule["price"],
                "per_million_requests_usd": round(reqd["price"] * 1_000_000, 4), "monthly_usd": round(monthly, 2),
                "yearly_usd": round(monthly * 12, 2), "currency": "USD", "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_route53_price(hosted_zones: int = 1, monthly_queries: float = 1_000_000) -> dict:
    """On-Demand Route 53 monthly cost = hosted zones + standard queries (global)."""
    conn = _db()
    try:
        dims = _dims(conn, "AmazonRoute53", "global")
        zone = _pick(dims, all_of=("hosted zone",)) or _pick(dims, all_of=("zone",))
        query = _pick(dims, any_of=("standard quer", "queries")) or _pick(dims, all_of=("quer",))
        if not zone or not query:
            return {"found": False, "error": "Could not locate Route 53 rates.",
                    "hint": "inspect_service_prices('AmazonRoute53', 'global')"}
        monthly = hosted_zones * zone["price"] + (monthly_queries / 1_000_000) * query["price"]
        return {"found": True, "service": "Route53", "scope": "global",
                "assumptions": {"hosted_zones": hosted_zones, "monthly_queries": monthly_queries},
                "hosted_zone_monthly_usd": zone["price"], "per_million_queries_usd": query["price"],
                "monthly_usd": round(monthly, 2), "yearly_usd": round(monthly * 12, 2),
                "currency": "USD", "data": _freshness(conn)}
    finally:
        conn.close()


@mcp.tool()
def get_cloudfront_price(monthly_gb_out: float, monthly_requests: float = 0,
                         region_group: str = "United States") -> dict:
    """On-Demand CloudFront monthly cost (data transfer out). region_group e.g.
    'United States','Europe','India','Asia'. Uses the base tier."""
    conn = _db()
    try:
        dims = _dims(conn, "AmazonCloudFront", "global")
        dto = _pick(dims, all_of=("data transfer", region_group.lower())) \
              or _pick(dims, all_of=("data transfer", "out"), none_of=("request",))
        if not dto:
            return {"found": False, "error": "Could not locate CloudFront rate.",
                    "hint": "inspect_service_prices('AmazonCloudFront', 'global', 'data transfer')"}
        monthly = monthly_gb_out * dto["price"]
        return {"found": True, "service": "CloudFront", "region_group": region_group,
                "assumptions": {"monthly_gb_out": monthly_gb_out, "monthly_requests": monthly_requests,
                                "note": "base-tier rate; deep volume tiers are cheaper"},
                "per_gb_out_usd": dto["price"], "monthly_usd": round(monthly, 2),
                "yearly_usd": round(monthly * 12, 2), "currency": "USD", "data": _freshness(conn)}
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
