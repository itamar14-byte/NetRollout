"""A page's rows as a CSV download (Results, the audit log): safe to open
in a spreadsheet, and read right by Excel."""
import csv
import io
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from flask import Response


# A spreadsheet runs a cell starting with these as a formula (CSV injection):
# such a cell gets a leading ' - the spreadsheet shows it as text
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
# Excel reads a CSV as UTF-8 only after a byte order mark
BOM = "﻿"


def cell(value: Any) -> str:
	""":returns: the value as a CSV cell: None as empty, text a spreadsheet
	 would run as a formula with a leading '"""
	text = "" if value is None else str(value)
	return "'" + text if text.startswith(FORMULA_STARTS) else text


def csv_download(name: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Response:
	"""The rows as a CSV file to download, named netrollout-<name>-<now>.csv.

	:param header: the first row, the columns' names"""
	out = io.StringIO()
	out.write(BOM)
	writer = csv.writer(out, lineterminator="\r\n")
	writer.writerow(header)
	writer.writerows([cell(value) for value in row] for row in rows)
	filename = f"netrollout-{name}-{datetime.now():%Y%m%d-%H%M%S}.csv"
	return Response(out.getvalue(), mimetype="text/csv",
	                headers={"Content-Disposition": f'attachment; filename="{filename}"'})
