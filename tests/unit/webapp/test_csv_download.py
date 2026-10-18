"""The CSV downloads (Results, the audit log): cells a spreadsheet can't run
as formulas, a byte order mark for Excel, the file's name."""
import csv
import io
import re

import pytest
from flask import Flask

from src.webapp.csv_download import cell, csv_download


@pytest.mark.parametrize("value, expected", [
	("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
	("+1", "'+1"), ("-cmd", "'-cmd"), ("@SUM(A1)", "'@SUM(A1)"),
	("\tTAB", "'\tTAB"), ("\rCR", "'\rCR"),
	("core-1", "core-1"), ("a=b", "a=b"), ("", ""), (None, ""), (7, "7"),
])
def test_a_cell_a_spreadsheet_would_run_gets_a_quote(value, expected):
	"""Text starting with = + - @ tab or CR gets a leading ' (shown as text);
	anything else is written as it is, None as empty."""
	assert cell(value) == expected


def test_the_download_is_utf8_with_a_bom_and_named_by_the_time():
	"""The file starts with a byte order mark (Excel reads UTF-8 only after
	one), holds the header then the rows (cells escaped), and is an
	attachment named netrollout-<name>-<YYYYMMDD-HHMMSS>.csv."""
	with Flask(__name__).test_request_context():
		resp = csv_download("results", ["a", "b"], [["שלום", "=1+1"], [None, 3]])
	text = resp.get_data(as_text=True)
	assert text.startswith("﻿")
	assert list(csv.reader(io.StringIO(text[1:]))) == [["a", "b"], ["שלום", "'=1+1"], ["", "3"]]
	assert resp.mimetype == "text/csv"
	assert re.fullmatch(r'attachment; filename="netrollout-results-\d{8}-\d{6}\.csv"',
	                    resp.headers["Content-Disposition"])
