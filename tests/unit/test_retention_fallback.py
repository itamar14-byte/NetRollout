"""When the app runs the nightly clean-up itself (no pg_cron): 03:00, once a
day, a missed time caught up once."""
from datetime import datetime

import pytest

from src.webapp.retention import due


@pytest.mark.parametrize("now, last_run, expected", [
	(datetime(2026, 10, 6, 2, 59), None, False),                       # not yet
	(datetime(2026, 10, 6, 3, 0), None, True),                         # at 03:00
	(datetime(2026, 10, 6, 10, 0), None, True),                        # started later: caught up
	(datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 6, 3, 1), False),  # done today
	(datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 5, 3, 1), True),   # yesterday's
	(datetime(2026, 10, 7, 2, 0), datetime(2026, 10, 6, 3, 1), False),   # tomorrow, before 03:00
])
def test_once_a_day_at_three(now, last_run, expected):
	assert due(now, last_run) is expected
