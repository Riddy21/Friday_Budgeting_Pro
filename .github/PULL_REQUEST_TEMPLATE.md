## Summary

<!-- One paragraph on what this PR does. -->

Closes #

## Scope

- [ ] This PR only touches files within the scope of the linked issue
- [ ] I have not added features outside ARCHITECTURE.md
- [ ] No new dependencies added (or: justified in description below)

## Tests

<!-- Describe what tests were added and how to run them locally. -->

- [ ] CI is green (`pytest -q`)
- [ ] New code has corresponding tests in `tests/`

## Architecture compliance

- [ ] Stayed within the design constraints in [ARCHITECTURE.md](../blob/main/ARCHITECTURE.md#design-constraints-read-this-first)
- [ ] No multi-user / multi-tenant logic
- [ ] No CLI / web UI additions
- [ ] No standalone scheduler logic (uses OpenClaw `cron` when scheduling is needed)
