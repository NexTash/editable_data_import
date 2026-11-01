# Copyright (c) 2026, Irfan Zahoor and contributors
# For license information, please see license.txt
"""Validating an edited dataset with the same logic the real import uses.

Two layers, deliberately kept distinct:

* **Parse validation** is entirely core's. Calling
  ``ImportFile.get_payloads_for_import()`` runs the stock Row/Column checks -
  link existence, Select options, date formats, Duration patterns - and leaves
  its findings on the Row and Column objects. This module only *reads* those
  findings and works out which grid cell each one belongs to, because core
  records a fieldname where the grid needs a column index.

* **Deep validation** replays ``Importer.insert_record`` inside a savepoint and
  rolls it back. That runs the real ERPNext ``validate()`` hooks and mandatory
  checks, which the parse layer cannot see: an empty mandatory field and a
  non-numeric Currency both pass parse validation untouched today.

A third, much smaller layer adds the checks core simply does not have. ``flt()``
turns "abc", "$100" and "N/A" into 0.0 without a murmur, so a typo silently
imports as zero. Those are reported as non-blocking warnings, never as blocking
errors, so that the client's idea of what blocks an import stays identical to
the server-side gate in ``Importer.import_data()``.
"""

import re

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import INVALID_VALUES, ImportFile
from frappe.core.doctype.data_import.value_mapping import (
	get_blocking_warnings,
	get_skipped_row_numbers,
	get_unmapped_invalid_values_for_column,
)
from frappe.utils import cint, cstr, flt
from frappe.utils.data import escape_html

NUMERIC_FIELDTYPES = {"Int", "Float", "Currency", "Percent"}
INTEGER_FIELDTYPES = {"Int"}

# Matches every spelling of zero we are willing to believe was intentional.
_ZERO_PATTERN = re.compile(r"^[-+]?0*(?:[.,]0*)?$")

DRY_RUN_SAVEPOINT = "edi_dry_run"
DEFAULT_DEEP_VALIDATE_LIMIT = 500

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def get_deep_validate_limit() -> int:
	configured = frappe.conf.get("edi_deep_validate_row_limit")
	try:
		configured = int(configured)
	except (TypeError, ValueError):
		return DEFAULT_DEEP_VALIDATE_LIMIT
	return configured if configured > 0 else DEFAULT_DEEP_VALIDATE_LIMIT


def collect_warnings(doc, import_file: ImportFile) -> tuple[list, set]:
	"""Core's warnings plus core's verdict on which of them block the import.

	Deciding this locally is not an option. ``Importer.import_data()`` gates on
	``get_blocking_warnings(import_file.get_all_warnings(), ...)``, and that
	function is considerably more than a ``type != "info"`` test: it drops
	warnings on rows the user marked as skipped, keeps a ``value_mapping``
	warning only while the column still has unmapped values, and lets a
	``duplicate_id`` through once every duplicate but the first is skipped.
	``get_all_warnings`` also adds the tree-structure warnings that
	``get_warnings`` leaves out.

	Re-deriving any of that here produces a preview that says "no blocking
	errors" on an import the server will refuse, or vice versa. So both calls are
	core's, and blocking membership is tracked by identity.

	Returns ``(warnings, blocking_ids)``.
	"""
	warnings = import_file.get_all_warnings()
	blocking = get_blocking_warnings(warnings, import_file, doc)
	return warnings, {id(warning) for warning in blocking}


def skipped_row_numbers(doc) -> set:
	"""Sheet rows the user excluded; the real import never builds a document for them."""
	return get_skipped_row_numbers(doc)


def _resolve_grid_column(columns: list, warning: dict, row_values: list):
	"""Work out which grid column a core warning is talking about.

	Date, Datetime and Duration warnings already carry ``col``. Link and Select
	warnings carry only the docfield, so the column has to be matched back by
	fieldname. When a doctype exposes the same fieldname through two different
	child tables the message text disambiguates, because core embeds the
	offending value in it.
	"""
	column_number = warning.get("col")
	if column_number:
		# Column.column_number is 1-based over file columns, and the grid
		# prepends Sr. No, so the grid index is the same number.
		return cint(column_number)

	field = warning.get("field") or {}
	fieldname = field.get("fieldname")
	if not fieldname:
		return None

	candidates = [
		col
		for col in columns
		if col.get("df")
		and col["df"].get("fieldname") == fieldname
		and col["df"].get("parent") == field.get("parent")
		and not col.get("skip_import")
	]
	if not candidates:
		return None
	if len(candidates) == 1:
		return candidates[0]["grid_index"]

	message = warning.get("message") or ""
	for col in candidates:
		file_index = col.get("file_index")
		if file_index is None or file_index >= len(row_values):
			continue
		value = row_values[file_index]
		if value in INVALID_VALUES:
			continue
		if escape_html(cstr(value)) in message:
			return col["grid_index"]

	return candidates[0]["grid_index"]


def _locate_value_mapping_cells(import_file: ImportFile, warnings: list, blocking_ids: set, skipped: set):
	"""Per-cell locators for core's column-level ``value_mapping`` warnings.

	An unmappable Link or Select value is the commonest import error there is,
	but core reports it once per *column*, with the offending row numbers written
	into the message text rather than carried as data. Left at that, the grid can
	only say "something in column 3 is wrong" and the user has to hunt for the
	cell.

	The rows are recoverable: core already computed the column's invalid items
	and cached them on the Column, so they are expanded back into coordinates
	here. Mapped values and skipped rows are excluded, using core's own filter,
	so a cell is only marked while it would really block the import.

	These are locators for a warning that is already counted, not findings of
	their own, so they carry a ``source`` marker that keeps them out of
	``blocking_count``.
	"""
	columns_by_number = {col.column_number: col for col in import_file.header.columns}
	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	located = []

	for warning in warnings:
		if warning.get("type") != "value_mapping" or id(warning) not in blocking_ids:
			continue

		col = columns_by_number.get(cint(warning.get("col")))
		if not col or not col.df:
			continue

		unmapped = get_unmapped_invalid_values_for_column(
			col, import_file.value_lookup, import_file.reference_doctype, skipped
		)
		target = col.df.options or col.df.label or col.header_title

		for item in unmapped:
			for row_number in item.get("rows") or []:
				row_number = cint(row_number)
				position = positions.get(row_number)
				if position is None or row_number in skipped:
					continue
				located.append(
					{
						"position": position,
						"row_number": row_number,
						# Column.column_number is 1-based and the grid prepends
						# Sr. No, so the grid index is the same number.
						"column": col.column_number,
						"fieldname": col.df.fieldname,
						"severity": SEVERITY_ERROR,
						"source": "value_mapping",
						"message": _("{0} is not a valid {1}. Correct it here, or map it under Fix Issues.").format(
							frappe.bold(escape_html(cstr(item.get("source")))),
							frappe.bold(escape_html(cstr(target))),
						),
					}
				)

	return located


def _is_unparseable_number(value) -> bool:
	"""True when flt() would silently swallow this value as 0."""
	text = cstr(value).strip()
	if not text:
		return False
	if flt(text) != 0:
		return False
	return not _ZERO_PATTERN.match(text.replace(",", "").replace(" ", ""))


def _loses_precision_as_int(value) -> bool:
	"""True when cint() drops a value that flt() reads perfectly well."""
	text = cstr(value).strip()
	if not text:
		return False
	return cint(text) == 0 and flt(text) != 0


def _check_numeric_cells(columns: list, import_file: ImportFile, already_flagged: set) -> list:
	"""The gap-filling layer: numbers core coerces to 0 without complaint.

	``already_flagged`` holds the ``(row_number, grid_column)`` pairs core has
	itself reported on. Core gained a per-cell "is not a valid number" check, so
	without this skip a cell like ``abc`` collects two messages - core's blocking
	error and a softer "will be imported as 0" note that contradicts it.

	What core still does not catch is truncation: ``0.5`` in an Int column parses
	as a perfectly good number, and ``cint()`` then silently makes it 0. That is
	the case this layer is left to cover.
	"""
	findings = []
	numeric_columns = [
		col
		for col in columns
		if col.get("df")
		and not col.get("skip_import")
		and col["df"].get("fieldtype") in NUMERIC_FIELDTYPES
	]
	if not numeric_columns:
		return findings

	for position, row in enumerate(import_file.data):
		values = row.as_list()
		for col in numeric_columns:
			file_index = col.get("file_index")
			if file_index is None or file_index >= len(values):
				continue
			value = values[file_index]
			if value in INVALID_VALUES:
				continue
			if (row.row_number, col["grid_index"]) in already_flagged:
				continue

			label = col["df"].get("label") or col["df"].get("fieldname")
			if _is_unparseable_number(value):
				findings.append(
					{
						"position": position,
						"row_number": row.row_number,
						"column": col["grid_index"],
						"fieldname": col["df"].get("fieldname"),
						"severity": SEVERITY_WARNING,
						"message": _("{0} is not a number and will be imported as 0 into {1}").format(
							frappe.bold(escape_html(cstr(value))), frappe.bold(escape_html(label))
						),
					}
				)
			elif col["df"].get("fieldtype") in INTEGER_FIELDTYPES and _loses_precision_as_int(value):
				findings.append(
					{
						"position": position,
						"row_number": row.row_number,
						"column": col["grid_index"],
						"fieldname": col["df"].get("fieldname"),
						"severity": SEVERITY_WARNING,
						"message": _("{0} is not a whole number and will be imported as 0 into {1}").format(
							frappe.bold(escape_html(cstr(value))), frappe.bold(escape_html(label))
						),
					}
				)

	return findings


def run_deep_validation(doc, import_file: ImportFile, payloads: list, limit: int) -> dict:
	"""Replay the real insert for each document, then roll it back.

	This is the only way to surface mandatory fields and ERPNext business rules
	before an import runs, because the parse layer never builds a Document. The
	steps mirror ``Importer.insert_record`` so that a pass here means the same
	thing a pass in the real import would.

	Everything happens inside a savepoint that is always rolled back, including
	on success, so no row survives the call.
	"""
	# The real import skips any payload that touches a skipped row, so replaying
	# one here would report a mandatory-field error for a document that is never
	# going to be inserted.
	skipped = skipped_row_numbers(doc)
	if skipped:
		payloads = [
			payload
			for payload in payloads
			if not skipped.intersection(row.row_number for row in payload.rows)
		]

	results = []
	checked = 0
	truncated = len(payloads) > limit

	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	meta = frappe.get_meta(doc.reference_doctype)
	is_update = doc.import_type == "Update Existing Records"

	previous_in_import = frappe.flags.in_import
	previous_mute = frappe.flags.mute_emails
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True

	try:
		for payload in payloads[:limit]:
			checked += 1
			row_numbers = [row.row_number for row in payload.rows]
			row_positions = [positions[n] for n in row_numbers if n in positions]

			frappe.db.savepoint(DRY_RUN_SAVEPOINT)
			try:
				frappe.clear_messages()
				if is_update:
					_dry_run_update(doc, payload.doc)
				else:
					_dry_run_insert(doc, meta, payload.doc)
			except Exception as exception:
				results.append(
					{
						"positions": row_positions,
						"row_numbers": row_numbers,
						"severity": SEVERITY_ERROR,
						"exception": type(exception).__name__,
						"message": _collect_message(exception),
					}
				)
			finally:
				# Unconditional: a document that validated cleanly must not
				# survive either.
				frappe.db.rollback(save_point=DRY_RUN_SAVEPOINT)
				frappe.clear_messages()
	finally:
		frappe.flags.in_import = previous_in_import
		frappe.flags.mute_emails = previous_mute

	return {"results": results, "checked": checked, "truncated": truncated, "limit": limit}


def _dry_run_insert(doc, meta, parsed_doc):
	"""Mirrors Importer.insert_record, minus the submit step."""
	new_doc = frappe.new_doc(doc.reference_doctype)
	new_doc.update(parsed_doc)

	if not parsed_doc.get("name") and (meta.autoname or "").lower() != "prompt":
		new_doc.set("name", None)

	new_doc.insert()
	return new_doc


def _dry_run_update(doc, parsed_doc):
	"""Mirrors Importer.update_record.

	A missing target is the failure the real import would hit too, so it is
	allowed to raise and be reported like any other error.
	"""
	from frappe.core.doctype.data_import.importer import get_id_field

	id_field = get_id_field(doc.reference_doctype)
	existing = frappe.get_doc(doc.reference_doctype, parsed_doc.get(id_field.fieldname))
	existing.update(parsed_doc)
	existing.save()
	return existing


def _collect_message(exception) -> str:
	"""Prefer Frappe's own user-facing message over the raw exception text."""
	messages = []
	for entry in frappe.local.message_log or []:
		if isinstance(entry, dict):
			message = entry.get("message")
		else:
			message = cstr(entry)
		if message:
			messages.append(message)

	if messages:
		return "<br>".join(messages)
	return escape_html(cstr(exception)) or _("Unknown error")


def analyse(doc, import_file: ImportFile, columns: list, deep: bool = False, deep_limit: int | None = None):
	"""Full picture of an edited dataset: cell errors, row errors, column warnings.

	``get_payloads_for_import()`` is called exactly once here. It is what makes
	core attach row-level warnings, and calling it twice on one ImportFile would
	duplicate every one of them.
	"""
	payloads = import_file.get_payloads_for_import()
	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	values_by_position = [row.as_list() for row in import_file.data]

	warnings, blocking_ids = collect_warnings(doc, import_file)
	skipped = skipped_row_numbers(doc)

	cell_errors = []
	row_errors = []
	column_warnings = []
	general_warnings = []

	for warning in warnings:
		severity = SEVERITY_ERROR if id(warning) in blocking_ids else SEVERITY_INFO
		row_number = warning.get("row")

		if row_number:
			position = positions.get(row_number)
			row_values = values_by_position[position] if position is not None else []
			grid_column = _resolve_grid_column(columns, warning, row_values)

			entry = {
				"position": position,
				"row_number": row_number,
				"severity": severity,
				"message": warning.get("message"),
				"fieldname": (warning.get("field") or {}).get("fieldname"),
			}
			if grid_column is not None:
				entry["column"] = grid_column
				cell_errors.append(entry)
			else:
				row_errors.append(entry)
			continue

		if warning.get("col"):
			column_warnings.append(
				{
					"column": cint(warning.get("col")),
					"severity": severity,
					"message": warning.get("message"),
				}
			)
			continue

		general_warnings.append({"severity": severity, "message": warning.get("message")})

	# Cell coordinates for the column-level warnings core cannot place itself.
	cell_errors.extend(_locate_value_mapping_cells(import_file, warnings, blocking_ids, skipped))

	# Gap-filling numeric checks, always non-blocking, and never on a cell core
	# has already spoken about.
	flagged_cells = {
		(entry["row_number"], entry["column"]) for entry in cell_errors if entry.get("row_number")
	}
	cell_errors.extend(_check_numeric_cells(columns, import_file, flagged_cells))

	deep_report = None
	deep_error_count = 0
	if deep:
		limit = deep_limit or get_deep_validate_limit()
		deep_report = run_deep_validation(doc, import_file, payloads, limit)
		for result in deep_report["results"]:
			for position in result["positions"]:
				row_errors.append(
					{
						"position": position,
						"row_number": import_file.data[position].row_number,
						"severity": SEVERITY_ERROR,
						"message": result["message"],
						"source": "deep",
					}
				)
				deep_error_count += 1

	# Every bucket counts, so that a warning core blocks on but the grid cannot
	# pin to a cell still shows up in the tally.
	#
	# Entries carrying a ``source`` are excluded, because none of them is a core
	# warning: "value_mapping" locators restate a column warning that is already
	# counted, and "deep" findings come from this app's own replay layer rather
	# than core's gate. Counting either would make the preview report a count
	# that the server's own refusal does not agree with.
	blocking = [
		entry
		for entry in cell_errors + row_errors + column_warnings + general_warnings
		if entry["severity"] == SEVERITY_ERROR and not entry.get("source")
	]

	return {
		"cell_errors": cell_errors,
		"row_errors": row_errors,
		"column_warnings": column_warnings,
		"general_warnings": general_warnings,
		"blocking_count": len(blocking),
		"deep_error_count": deep_error_count,
		"payload_count": len(payloads),
		"deep": deep_report,
	}
