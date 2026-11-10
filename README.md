# NetRollout download counts

`downloads.csv`, a row per day and file, added by `.github/workflows/stats.yml` on master:

- `date`: the day counted (UTC)
- `source`: `github` (a release file's downloads) or `dockerhub` (an image's pulls)
- `name`: `<tag>/<file>`, or the image
- `count`: the running total that day - a day's downloads are the difference from the day before

The counts include upgrades (NetRollout Manager and the Linux upgrade download the same files).

`traffic.csv`, the repository's traffic from GitHub (it keeps only the last 14 days, so the history is here):

- `date`: for `views` / `clones`, the day measured (UTC; the last days are refreshed on every run); for `referrer` / `path`, the day fetched
- `metric`: `views` (the repository's pages), `clones` (git clones), `referrer` (the sites people came from), `path` (the pages viewed most)
- `name`: the referrer or the path; empty for views and clones
- `count`, `uniques`: that day's total and distinct visitors; for referrers and paths, the 14 days before the fetch

Clones include CI's own checkouts (every workflow run clones the repository), so `views` uniques are the better measure of people.
