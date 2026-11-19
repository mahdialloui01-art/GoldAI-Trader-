from pathlib import Path
import csv, json, hashlib

FILES = [
    "xauusd-m1-bid-2026-05-01-2026-08-01.csv",
    "xauusd-m1-ask-2026-05-01-2026-08-01.csv",
]
out = {"status": "inspection", "files": []}
for name in FILES:
    p = Path(name)
    if not p.exists():
        out["files"].append({"name": name, "exists": False})
        continue
    size = p.stat().st_size
    item = {"name": name, "exists": True, "bytes": size}
    with p.open("rb") as f:
        first = f.read(256)
    item["sha256_prefix"] = hashlib.sha256(p.read_bytes()[:1024*1024]).hexdigest()
    item["first_bytes"] = first.decode("utf-8-sig", errors="replace")
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            sample = list(csv.reader(f))[:4]
        item["sample_rows"] = sample
        item["column_count"] = len(sample[0]) if sample else 0
    except Exception as e:
        item["csv_error"] = repr(e)
    out["files"].append(item)
out["ready_for_backtest"] = all(x.get("exists") and x.get("bytes", 0) > 100 for x in out["files"])
Path("data-inspection.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(out, indent=2, ensure_ascii=False))
if not out["ready_for_backtest"]:
    raise SystemExit("Market data files are missing, empty, or unresolved Git LFS pointers.")
