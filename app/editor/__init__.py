"""AI editor: open a drawing next to a chat and edit it by asking.

This package is separate from the converter. It only borrows the converter's
file reader (``converter._load``) to open DWG/DXF, and its job queue to export
the edited drawing to an older AutoCAD version.

The language model never touches the drawing. It answers with a reply plus a
list of operations drawn from a fixed vocabulary (``ops.py``). Every operation
is validated and applied to a *copy* of the drawing; the person sees exactly
what would change and accepts or rejects it.
"""
