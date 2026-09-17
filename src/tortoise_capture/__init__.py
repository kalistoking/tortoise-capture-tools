"""tortoise-capture-tools: WoW 1.12.1 world-session capture decoder.

Layering (see ARCHITECTURE.md): wire -> core -> modules -> emit. No layer
imports one above it, and nothing outside `modules/` imports `modules/`.
"""

__version__ = "0.1.0"
