## What and why

<!-- What does this change, and why is it needed? -->

Closes #

## Checklist

- [ ] Tests added or updated, and each new test fails without this change
- [ ] Any changed test says why in the commit message (no loosened assertions, no skip/xfail to get green)
- [ ] `python -m mypy` passes
- [ ] A migration for any model change (`alembic revision --autogenerate`)
- [ ] An entry in `CHANGELOG.md` under **Unreleased**, if a user would notice the change
- [ ] Documentation updated, if behaviour, a command or a setting changed
- [ ] CI is green

See [CONTRIBUTING.md](https://github.com/itamar14-byte/NetRollout/blob/master/CONTRIBUTING.md) for the details.
