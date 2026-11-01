# Copyright (c) 2026, Irfan Zahoor and contributors
# For license information, please see license.txt
"""Bridging the stock Frappe import pipeline and the editable preview grid.

Everything the grid needs - column metadata, row values, warnings, payload
grouping - already exists inside ``frappe.core.doctype.data_import.importer``.
The only capability core does not offer is building an ``ImportFile`` from rows
that exist in a browser rather than in a file, because its ``__init__`` always
reads from a File document, a filesystem path or a Google Sheet.

``reparse_with_raw_data`` closes exactly that one gap and nothing else. It does
so by *cloning* an ImportFile core itself constructed and swapping only
``raw_data`` before re-parsing, rather than by re-listing the attributes core's
``__init__`` sets. That matters: core's ``__init__`` grew ``value_lookup``,
``reference_doctype``, ``column_to_date_format_map`` and
``tree_parent_overrides``, every one of which ``parse_data_from_template`` reads.
A hand-maintained attribute list silently falls behind on upgrade and surfaces
as an AttributeError mid-parse; copying ``__dict__`` carries new attributes over
for free. Header, Column and Row behaviour, date-format guessing, warning
collection and payload grouping therefore remain byte-for-byte the stock
implementation.
"""

import frappe
from frappe.core.doctype.data_import.importer import INVALID_VALUES, Importer, ImportFile

# The stock preview ships 10 rows. That limit is a payload-size guard, not a
# rendering guard - frappe-datatable virtualises rows through HyperList, so the
# DOM only ever holds what is on screen. 2000 rows measures at roughly 0.4 MB of
# JSON, which is a comfortable default; larger files are paged rather than
# truncated. Override with "edi_max_editable_rows" in site_config.json.
DEFAULT_MAX_EDITABLE_ROWS = 2000

SR_NO_HEADER = "Sr. No"


def get_max_editable_rows() -> int:
	configured = frappe.conf.get("edi_max_editable_rows")
	try:
		configured = int(configured)
	except (TypeError, ValueError):
		return DEFAULT_MAX_EDITABLE_ROWS
	return configured if configured > 0 else DEFAULT_MAX_EDITABLE_ROWS


def reparse_with_raw_data(source: ImportFile, raw_data: list) -> ImportFile:
	"""A clone of ``source`` that parses ``raw_data`` instead of the file's own rows.

	Everything core's ``__init__`` configured - the field map, the value-mapping
	lookup, the reference doctype, per-column date formats, tree overrides,
	delimiter settings - is carried across verbatim, so the edited dataset is
	parsed under exactly the settings the real import will use. ``source`` is
	left untouched.
	"""
	edited = ImportFile.__new__(type(source))
	edited.__dict__.update(source.__dict__)

	# Core memoises derivations of the *source* rows on the instance under
	# underscore-prefixed names (_provider_warnings, _tree_preview_cache). The
	# clone parses different rows, so every one of them is stale. Dropping the
	# whole prefix rather than a fixed list keeps a cache added upstream later
	# from leaking a stale verdict into the edited dataset.
	for attribute in [key for key in edited.__dict__ if key.startswith("_")]:
		del edited.__dict__[attribute]

	# Warnings are per-parse; sharing the list would append to source's findings.
	edited.warnings = []
	edited.raw_data = raw_data
	edited.parse_data_from_template()
	return edited


def read_source_file(doc) -> ImportFile:
	"""Parse the file currently attached to the Data Import, using stock core logic."""
	doc.set_delimiters_flag()
	return Importer(
		doc.reference_doctype, data_import=doc, use_sniffer=doc.use_csv_sniffer
	).import_file


def normalise_cell(value):
	"""Match what core's CSV reader produces: trimmed strings, blanks as None."""
	if value is None:
		return None
	if isinstance(value, list | dict | tuple | set):
		# A grid cell is always a scalar. A container here means a malformed
		# payload, and writing its repr into the file would corrupt the import.
		return None
	if isinstance(value, str):
		value = value.strip()
		return value or None
	return value


def apply_edits_to_raw_data(import_file: ImportFile, edits: dict) -> list:
	"""Overlay a sparse edit map onto a copy of the parsed file's raw rows.

	``edits`` is ``{row_position: {column_index: value}}`` where row_position is
	the index into ``import_file.data`` (the grid's row order, blank file rows
	already skipped) and column_index is the 0-based file column, excluding the
	grid's synthetic "Sr. No" column.

	Returns a new raw_data list; the ImportFile passed in is never mutated.
	"""
	raw_data = [list(row) if row is not None else [] for row in import_file.raw_data]
	column_count = len(import_file.columns)

	for position, changes in (edits or {}).items():
		try:
			position = int(position)
		except (TypeError, ValueError):
			continue
		if not (0 <= position < len(import_file.data)):
			continue
		if not isinstance(changes, dict):
			continue

		# Row.index is the index into raw_data, so blank rows and a header that
		# is not on line one are both handled without any extra bookkeeping.
		raw_index = import_file.data[position].index
		row = raw_data[raw_index]
		if len(row) < column_count:
			row.extend([None] * (column_count - len(row)))

		for column_index, value in changes.items():
			try:
				column_index = int(column_index)
			except (TypeError, ValueError):
				continue
			if 0 <= column_index < len(row):
				row[column_index] = normalise_cell(value)

	return raw_data


def build_edited_import_file(doc, edits: dict, source: ImportFile | None = None) -> ImportFile:
	"""The edited dataset, re-parsed through the stock pipeline end to end."""
	source = source or read_source_file(doc)
	if not edits:
		return source

	raw_data = apply_edits_to_raw_data(source, edits)
	return reparse_with_raw_data(source, raw_data)


def serialise_columns(import_file: ImportFile) -> list:
	"""Column metadata for the grid, mirroring core's preview payload.

	Core trims the docfield down to the handful of keys the read-only preview
	needs. The editable grid builds real Frappe controls from these, so a few
	more keys are carried - but still not the whole docfield, to keep the
	payload small.
	"""
	columns = [frappe._dict({"header_title": SR_NO_HEADER, "skip_import": True, "editable": False})]
	columns += [col.as_dict() for col in import_file.columns]

	for index, col in enumerate(columns):
		col.grid_index = index
		# file_index is the position in a file row; the Sr. No column has none.
		col.file_index = index - 1 if index else None
		if col.df:
			col.df = {
				"fieldtype": col.df.fieldtype,
				"fieldname": col.df.fieldname,
				"label": col.df.label,
				"options": col.df.options,
				"parent": col.df.parent,
				"reqd": col.df.reqd,
				"default": col.df.default,
				"read_only": col.df.read_only,
				"precision": col.df.get("precision"),
				"non_negative": col.df.get("non_negative"),
			}
			col.editable = not col.skip_import and not col.df.get("read_only")
		else:
			col.editable = False

		# Columns that belong to a child table may only be edited on the row
		# that owns them; see get_row_structure().
		col.is_parent_column = bool(col.df and col.df.get("parent") == import_file.doctype)

	return columns


def get_row_structure(import_file: ImportFile) -> dict:
	"""Which grid rows start a document and which continue the previous one.

	A child-table import puts one document across several file rows: the first
	carries the parent columns, the rest leave them blank. Core decides this in
	``parse_next_row_for_import``; rather than re-deriving the rule, this asks
	core for the payloads and reads the grouping back off them, so the grid can
	never disagree with the importer about where a document starts.
	"""
	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	continuation = []
	doc_start = []

	payloads = import_file.get_payloads_for_import()
	for payload in payloads:
		for offset, row in enumerate(payload.rows):
			position = positions.get(row.row_number)
			if position is None:
				continue
			(doc_start if offset == 0 else continuation).append(position)

	return {
		"continuation_rows": sorted(continuation),
		"document_start_rows": sorted(doc_start),
		"payload_count": len(payloads),
		"positions": positions,
	}


def serialise_rows(import_file: ImportFile, start: int = 0, limit: int | None = None) -> list:
	"""Grid rows: a synthetic Sr. No column followed by the file's own values.

	Sr. No carries the physical file row number, exactly as the stock preview
	does, so warnings, the import log and "Export Errored Rows" all keep
	pointing at the same numbers the user already sees today.
	"""
	rows = import_file.data
	end = len(rows) if limit is None else min(start + limit, len(rows))
	return [[row.row_number, *row.as_list()] for row in rows[start:end]]


def is_row_blank(values) -> bool:
	return all(value in INVALID_VALUES for value in values)
