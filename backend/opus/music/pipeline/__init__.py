"""The music pipeline, one module per phase: choose (which release on which
channel) → grab (start the download; settle one that ends without an import) →
download (poll it) → arrival, judge, importer (what came, whether it is what was
wanted, filing it). monitor and records are the passes that run beside it.
Runs as asyncio tasks inside the API process — single-user app, no external
queue needed."""
