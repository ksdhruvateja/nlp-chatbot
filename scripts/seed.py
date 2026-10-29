"""Seed the database: run migrations, ingest the sample knowledge base, add sample
products and a demo customer/admin with a property. Safe to re-run.

    python -m scripts.seed
"""
from __future__ import annotations

from pathlib import Path

from app import db, ingestion
from app.security import issue_token

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "sample_knowledge"

DOCS = [
    ("running_toilet.md", "Running Toilet Troubleshooting Guide", "plumbing", None, []),
    ("leaky_faucet.md", "Repairing a Dripping Faucet", "plumbing", None, []),
    ("water_heater_general.md", "Tank Water Heater Troubleshooting", "plumbing", None, []),
    ("rheem_xe50_electric.md", "Rheem XE50T06ST45U1 Service Notes", "plumbing", "Rheem", ["XE50T06ST45U1"]),
    ("gfci_outlets.md", "GFCI Outlet Troubleshooting", "electrical", None, []),
    ("dishwasher_not_draining.md", "Dishwasher Not Draining - Service Guide", "appliance", None, []),
]

# SAMPLE products/prices for development only - replace with your catalog/pricing feed.
PRODUCTS = [
    ("SAMPLE-FLAP-2", "Universal 2 in. toilet flapper", None, "plumbing", "toilet_flapper", [], 699),
    ("SAMPLE-FLAP-3", "Universal 3 in. toilet flapper", None, "plumbing", "toilet_flapper", [], 1199),
    ("SAMPLE-FILL-U", "Universal adjustable toilet fill valve", None, "plumbing", "fill_valve", [], 1249),
    ("SAMPLE-CART-1", "Single-handle faucet cartridge", None, "plumbing", "faucet_cartridge", [], 1899),
    ("SAMPLE-ANODE-AZ", "Aluminum-zinc anode rod 44 in.", None, "plumbing", "anode_rod", [], 3499),
    ("SAMPLE-ELEM-4500", "Water heater element 4500W 240V screw-in", "Rheem", "plumbing", "heating_element",
     ["XE50T06ST45U1"], 2799),
    ("SAMPLE-ELEM-UNIV", "Universal water heater element 4500W 240V", None, "plumbing", "heating_element", [], 1999),
    ("SAMPLE-ELEM-WRENCH", "Water heater element wrench 1-1/2 in.", None, "plumbing", "element_wrench", [], 999),
    ("SAMPLE-GFCI-15", "15A tamper-resistant GFCI outlet", None, "electrical", "gfci_outlet", [], 2199),
    ("SAMPLE-GFCI-20", "20A tamper-resistant GFCI outlet", None, "electrical", "gfci_outlet", [], 2599),
    ("SAMPLE-DW-HOSE", "Universal dishwasher drain hose 6 ft", None, "appliance", "dishwasher_drain_hose", [], 1499),
]


def main() -> None:
    db.migrate()
    for filename, title, cat, brand, models in DOCS:
        result = ingestion.ingest((KB / filename).read_bytes(), filename, ingestion.DocumentMeta(
            title=title, source=f"sample://fixbridge/{filename}", permission="internal",
            approved_by="seed-script", service_category=cat, version="1.0", publisher="FixBridge (sample)",
            brand=brand, model_numbers=models))
        print(f"  {filename}: {result['status']} ({result['passages']} passages)")

    with db.connection() as conn:
        for sku, name, brand, cat, part, models, cents in PRODUCTS:
            conn.execute("""INSERT INTO products (sku, name, brand, service_category, part_type, compatible_models,
                                price_cents, price_source, price_verified_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'sample-seed', now())
                            ON CONFLICT (sku) DO UPDATE SET price_cents = excluded.price_cents,
                                price_verified_at = now()""", (sku, name, brand, cat, part, models, cents))
        admin = conn.execute("""INSERT INTO users (email, name, role) VALUES ('admin@example.com', 'Admin', 'admin')
                                ON CONFLICT (email) DO UPDATE SET role = 'admin' RETURNING id""").fetchone()
        cust = conn.execute("""INSERT INTO users (email, name) VALUES ('demo@example.com', 'Demo Customer')
                               ON CONFLICT (email) DO UPDATE SET name = excluded.name RETURNING id""").fetchone()
        prop = conn.execute("SELECT id FROM properties WHERE owner_id = %s AND name = 'Demo House'",
                            (cust["id"],)).fetchone()
        if not prop:
            prop = conn.execute(
                """INSERT INTO properties (owner_id, name, address, property_type, year_built, passport)
                   VALUES (%s, 'Demo House', '1 Example Way', 'house', 1998, %s) RETURNING id""",
                (cust["id"], db.jsonb({"occupancy": "owner", "water_heater_fuel": "electric",
                                       "plumbing": "copper/PEX", "panel": "200A"}))).fetchone()
            conn.execute("""INSERT INTO equipment (property_id, service_category, kind, brand, model_number,
                                install_year) VALUES (%s, 'plumbing', 'water heater', 'Rheem', 'XE50T06ST45U1', 2016)""",
                         (prop["id"],))
    print(f"\nDemo customer token:\n  {issue_token(cust['id'])}\nAdmin token:\n  {issue_token(admin['id'])}")


if __name__ == "__main__":
    main()
