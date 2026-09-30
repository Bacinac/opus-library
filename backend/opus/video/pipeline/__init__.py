"""The video pipeline, one module per phase: search (ask the indexers, keep
what is this film or episode) and score (what each release is worth) → grab
(start the download; take the next release when one fails) → download (poll
it) → importer (the acceptance test: probe the result, evaluate the subtitle
policy, fetch missing languages, and mark the item complete only when the
policy is satisfied — partial is never green) with naming for where it lands.
items holds what the pipeline works for; monitor and profiles are the passes
that run beside it."""
