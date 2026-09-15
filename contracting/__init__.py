
__version__ = '0.0.1'

# FR-11: process-wide monkeypatch, not a hooks.py doc_events/override entry -
# check_if_advance_entry_modified is a bare core utility function with no
# Frappe-sanctioned override point. See utils/erpnext_advance_patch.py for
# why and what.
from contracting.contracting.utils.erpnext_advance_patch import apply as _apply_erpnext_advance_patch

_apply_erpnext_advance_patch()
