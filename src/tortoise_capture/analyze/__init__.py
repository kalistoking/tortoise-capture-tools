"""Analyzers: what the session as a whole says, not what one packet says.

One file per question, discovered the same way opcode modules are. An analyzer
sees every decoded event in order, keeps its own state, and emits findings when
the stream ends -- as ordinary Events, so they reach the text and SQL sinks
through the same contracts a module uses.

Findings are observations about the capture, not authored content. Turning
them into world-database rows is the authoring emitter's job, deliberately
kept separate (see docs/feasibility-ralthas-pr.md).
"""
