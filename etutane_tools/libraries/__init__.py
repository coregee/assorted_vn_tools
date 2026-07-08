"""
tinkerbell  Handles the Tinkerbell/Cyberworks TOC+data archive format and LZSS.
scenetext   Handles .a0 scenario scripts <-> editable JSON (with EN line wrapping).
assets      Handles generic archive dump/substitution repack.
cyberworks  Handles the Cyberworks "AImage" image codec (decode/encode, PNG bridge).
png         Minimal 8-bit PNG read/write (no third-party deps).
tinkaudio   Handles Tink/Song header (de/en)cryption of the Ogg audio streams.
exepatch    Handles the Game.exe byte patches (registry gate, locale, half-width).
workspace   Handles paths, archive discovery, .orig backups, and overall flows.
"""
from . import (tinkerbell, scenetext, assets, cyberworks, png, tinkaudio,  # noqa: F401
               exepatch, workspace)
