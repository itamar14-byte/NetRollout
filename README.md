# NetRollout download counts

`downloads.csv`, a row per day and file, added by `.github/workflows/stats.yml` on master:

- `date`: the day counted (UTC)
- `source`: `github` (a release file's downloads) or `dockerhub` (an image's pulls)
- `name`: `<tag>/<file>`, or the image
- `count`: the running total that day - a day's downloads are the difference from the day before

The counts include updates (NetRollout Manager and the Linux update download the same files).
