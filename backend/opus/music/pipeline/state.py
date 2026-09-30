"""What the UI sees of the pipeline while it works. Single process, so plain
module state."""

# per-download file states, and during the search phase which channel a release
# is being searched on
live: dict[int, dict] = {}
searching: dict[int, str] = {}
# imports in flight — the poller retries stranded IMPORTING rows, and must not
# start a second import of one that is simply still running
importing: set[int] = set()
