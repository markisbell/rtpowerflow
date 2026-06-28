"""Import external grid models into netzsim's five-file input format.

Currently supports the *European Archetype Distribution Grid Models* LV
workbooks (``network_*.xlsx``), whose sheets (Nodes / Lines / Line Types /
Transformers / Loads) are translated into the pandapower-native JSON that
``netzsim.data_loader`` consumes.
"""
from .xlsx import (
    GridInputs,
    TrafoDefaults,
    convert_from_zip,
    convert_workbook,
    convert_xlsx_bytes,
    convert_xlsx_file,
    write_inputs,
)

__all__ = [
    "GridInputs",
    "TrafoDefaults",
    "convert_from_zip",
    "convert_workbook",
    "convert_xlsx_bytes",
    "convert_xlsx_file",
    "write_inputs",
]
