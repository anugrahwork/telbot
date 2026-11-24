# Contributing

Thanks for helping improve **Telbot**!

## Local Development Setup

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements-dev.txt
```

## Running Tests & Checks

All tests run offline without real Telegram or LLM API keys:

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py telbot tests
```

If you have `pytest` and `ruff` installed:

```bash
pytest -v
ruff check .
```

## Pull Request Guidelines

- Keep `telbot/` modular and well-typed.
- Never hardcode API keys, tokens, personal Telegram IDs, or private URLs.
- Add or update unit tests in `tests/` for any bug fix or new feature.
- Update `.env.example` and `README.md` if you introduce a new configuration option.
