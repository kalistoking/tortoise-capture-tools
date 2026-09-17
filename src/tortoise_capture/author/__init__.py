"""Authoring rules: one file per world table this toolkit can propose rows for.

Each rule is a sink over the whole stream -- decoded events and analyzer
findings alike -- that produces rows for its own table once the stream ends.
Registration and discovery work exactly as they do for `modules/` and
`analyze/`, so a new target table costs one file.

The output is a proposal for a human to review and apply, never a migration
that runs itself, and every value carries where it came from. See
docs/feasibility-ralthas-pr.md for what that distinction is worth.
"""
