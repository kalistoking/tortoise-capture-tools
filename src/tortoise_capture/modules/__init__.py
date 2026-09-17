"""Opcode modules -- the part of the codebase that grows.

One file per opcode (or per closely related pair). Each declares itself with
`@module(id=..., opcodes=(...))` and is discovered at startup; nothing else in
the codebase names it. See docs/adding-an-opcode.md.

A module may implement, in order of how often it is worth doing:
  decode()      required -- bytes to facts
  text_*        optional -- the printed form
  sql_*         optional -- the stored form
  expand()      optional -- only for container opcodes
"""
