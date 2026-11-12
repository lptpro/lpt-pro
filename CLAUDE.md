# Notes for AI coding assistants

Read [docs/DEVELOPING.md](docs/DEVELOPING.md) first. Its ground rules are not optional.

- Explain changes in plain, non-technical language. The maintainer prefers seeing results
  (screenshots, test output) over descriptions.
- Don't mention other products by name in code, comments or docs, and don't add personal
  names anywhere in the repository.
- Bump `__version__` in `lptpro/__init__.py` for every build handed out, and add a line to
  `CHANGELOG.md`.
- Test with `python -m lptpro --demo <folder>` (and `QT_QPA_PLATFORM=offscreen` for
  screenshots) before handing out a build.
