"""Patch: remove `priority` column from TAP Response Knowledge table if present.

This is idempotent and safe to run on installations that already dropped the column.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    try:
        table_name = "tabTAP Response Knowledge"
        # Check if column exists
        cols = [r[0] for r in frappe.db.sql(f"SHOW COLUMNS FROM `{table_name}`", as_dict=False)]
        if "priority" in cols:
            frappe.db.sql(f"ALTER TABLE `{table_name}` DROP COLUMN `priority`")
            frappe.db.commit()
            frappe.msgprint("Dropped `priority` column from TAP Response Knowledge.")
    except Exception as e:
        # Log and continue; patch should not raise on missing table/permission differences
        frappe.log_error(f"drop_priority_from_tap_response_knowledge failed: {e}", "tap_ai.patch")
