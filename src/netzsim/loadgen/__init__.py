"""Realistic load assignment from the cached LPG archetype library.

The runtime never calls the Load Profile Generator; it reads the pre-built JSON
library under ``data/lpg_library/`` (see ``scripts/build_lpg_library.py``) and
assigns/​scales those daily household profiles onto a grid's load elements.
"""
from .assign import AssignPolicy, assign_to_loads
from .ev import EvPolicy, assign_ev
from .library import Archetype, LoadLibrary
from .pv import PvPolicy, assign_pv

__all__ = [
    "Archetype", "LoadLibrary", "AssignPolicy", "assign_to_loads",
    "PvPolicy", "assign_pv", "EvPolicy", "assign_ev",
]
