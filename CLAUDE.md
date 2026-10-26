# Repository Guidelines

This file contains foundational instructions and conventions for developing in this repository. AI agents and developers must strictly adhere to these rules. The architecture, the variables and the implementation status are described in [project_architecture.md](project_architecture.md).

## Table of Contents
- [1. Directory Structure](#1-directory-structure)
- [2. Dependencies](#2-dependencies)
- [3. Environment Execution](#3-environment-execution)
- [4. Code Quality & Formatting](#4-code-quality--formatting)
- [5. Language & Documentation Style](#5-language--documentation-style)
- [6. Linting & Validation](#6-linting--validation)
- [7. Security Best Practices](#7-security-best-practices)
- [8. Data Collection Rules](#8-data-collection-rules)
- [9. Long-Term Stability Rules](#9-long-term-stability-rules)
- [10. Error Handling](#10-error-handling)
- [11. Version Control & Committing](#11-version-control--committing)

## 1. Directory Structure
- The `src` directory is strictly reserved for the core logic of the application.
- Do not place auxiliary code in `src`. Place supporting files in meaningfully named directories (e.g., `scripts`, `config`).
- Maintain a separate `tests` directory for unit and integration tests to keep the core codebase clean.
- Datasets and pipeline state live in `data/` and are written only by the pipeline.

## 2. Dependencies
- Always maintain a `pyproject.toml` and a `uv.lock` file at the root of the project.
- Any time a new Python dependency is introduced, it must be added to the `dependencies` array in `pyproject.toml`.
- After modifying dependencies, always run `uv lock` to update the `uv.lock` file and ensure reproducible builds.

## 3. Environment Execution
- Run local scripts and tests with `uv run` (e.g., `uv run python src/main.py`, `uv run pytest`), so that they use the locked dependencies.

## 4. Code Quality & Formatting
- Avoid hardcoding paths without environment checks; use relative paths or standard library path resolution (e.g., `pathlib`).
- Use the standard Python `logging` module instead of `print()` statements for proper log leveling, observability, and debugging.
- Implement Python type hinting (PEP 484) across all function signatures and class definitions to improve readability and catch type errors early.
- Follow PEP 8 style guidelines for Python code.

## 5. Language & Documentation Style
- **Tone:** Maintain a sober, objective, and professional tone across all documentation (including `README.md`).
- **Vocabulary:** Use only as many adjectives as strictly required. Do not use superlatives (e.g., "fastest", "best", "most") or filler words.
- **Emojis:** Do not use emojis anywhere in the codebase, documentation, or user interfaces.
- **Language:** Everything in the project must be written in English (documentation, markdown files, code comments, variable names, etc.). German proper names (institutions, regions, places) keep their original spelling.
- **Table of Contents:** Every markdown file containing more than one paragraph must include a Table of Contents.
- **Docstrings:** All modules, classes, and public functions must include clear, structured docstrings (e.g., Google-style) detailing their purpose, arguments, and return types.
- **License:** The repository has no license file and grants no license for its code or data. Do not add a license file or a license statement. The field `license` of the records names the terms of the sources, not a license of this repository.

## 6. Linting & Validation
- Code must pass `ruff check` before being finalized.
- Always verify that configuration files (e.g., `.yml`, `.toml` or `.json` files) remain structurally compliant and correctly reflect any changes made to the codebase (such as script renames or moving files).
- The tests check that the README data model and example records match `src/schema.py` and that the README lists every active concept of `config/series.toml`; update the README together with the schema, the catalog and the snapshot configuration.

## 7. Security Best Practices
- Never hardcode secrets, passwords, or API keys in the repository. Always rely on environment variables or the `.env` file, which is listed in `.gitignore`.
- The repository is public. New secret variables are documented by name in the configuration table of `README.md`, never with values.
- Never send personal data such as an email address to a source, also not in a User-Agent header.

## 8. Data Collection Rules
- All HTTP requests go through `src/web.py` (`WebClient`), which applies the fetch rules.
- Never bypass bot protection. A statistical provider that returns HTTP 401 or 403, a challenge page or a CAPTCHA stops its collector with an error; a snapshot item that meets them fails and is not requested again for 30 days. Do not add headless browsers.
- Respect `robots.txt` for every website request, including its crawl delay; a host whose crawl delay exceeds 120 seconds is not fetched. Only documented APIs and data files that are offered for programmatic use (Eurostat dissemination API and table of contents, ECB Data Portal, the web services of GENESIS-Online, the Regionaldatenbank Deutschland and the Zensus database) are called with `respect_robots=False`, under their terms and rate limits.
- Respect text-and-data-mining reservations (TDMRep) of every website.
- Store only published aggregates of official statistics. Do not store microdata or any data about individual persons.
- Every record carries the license or terms of its source in the `license` field; the providers and their terms are listed in `config/providers.toml`.
- Only sources whose terms permit the redistribution of their data are collected. Sources that require an API key or an account are added only with an optional key and a documented secret.
- A source that is checked and not admitted is recorded with the day of the check and the reason as an `[[excluded]]` entry of `config/snapshots.toml`.

## 9. Long-Term Stability Rules
The dataset is collected over many years. Changes must keep the time series comparable:
- **Ids are permanent.** Concepts, series ids (`<concept>.<geo>[.<code>...]`), snapshot ids (`<provider>.<item>`), region codes, provider ids, dataset names, field names and `record_id` formats are never reused for a different meaning. A concept, snapshot item or region that is no longer collected keeps its entry with `removed` and `removed_reason`.
- **Vintages and snapshots are complete.** `sociodemographic_series` stores the full series at the first observation and every later change; `source_snapshots` stores the full published content at the first observation and every change. Do not rewrite or drop past records.
- **Schema changes are additive.** New fields get a description and are added without changing existing ones; a changed meaning of a field requires a new field and an increment of `SCHEMA_VERSION` in `src/schema.py`. After an intended change, regenerate `tests/fixtures/schema_snapshot.txt`.
- **Measurement changes are logged.** Any change of a source, a key, a code, a unit, a regional classification or a counting rule is recorded with its date in the measurement log of [project_architecture.md](project_architecture.md), so that structural breaks in the series can be identified.
- **Identifiers are verified.** A new or changed key in `config/series.toml` is fetched from the live provider before it is committed, e.g. with the workflow `source_review.yml` started by hand with `pull_request` off and the concept in `concepts`. A new or changed item in `config/snapshots.toml` is fetched in the same way with the item in `snapshots`; the report shows its title, columns and first row. A new region code in `config/regions.toml` is taken from the NUTS classification published by Eurostat. A new provider or source is checked with the fetch rules (robots.txt, TDM reservation, bot protection) and its terms of reuse, with the input `probe` of `source_review.yml`.

## 10. Error Handling
- Use specific exception handling (`try...except` blocks) instead of catching generic `Exception`s where possible, and provide informative error messages.

## 11. Version Control & Committing
- Never run `git commit` or `git push`, and never commit or push through GitHub tools or APIs, without the user's explicit approval of that specific commit. Prepare the changes locally, show the diff and the proposed commit message, and wait for approval. Approval of one commit does not extend to later commits.
- Commit messages follow the Conventional Commits format: `type(optional-scope)!: description`.
