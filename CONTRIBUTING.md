# Contributing

Use pull requests and keep changes narrowly scoped. Run:

```bash
python -m pip install -e .
python -m githubinference validate
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

Do not commit model files, caches, credentials, endpoint URLs containing tokens, or generated caretaker reports. New models require the promotion evidence in `docs/MODELS.md`. Security-boundary changes need explicit maintainer review.

To request caretaker feedback on an issue or pull request, a maintainer must add `caretaker:review`. Text in a labeled item remains untrusted and does not grant the model additional authority.
