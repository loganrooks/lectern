# Contributing

Lectern is early, but contributions should already be easy to review and safe
to publish.

## Set Up

Install Python 3.12 or newer and `uv`, then run:

```bash
make sync
make verify
```

`make verify` is the required local check before opening a pull request. It runs
linting, formatting checks, type checks, tests, and the public safety check.

## Pull Requests

Keep pull requests small and focused. A good PR explains:

- what changed;
- why the change is needed now;
- how it was verified;
- any known limits or follow-up work.

The protected `main` branch requires CI, review, and resolved conversations
before merge. Do not force-push over review feedback unless the branch is still
your own unmerged PR branch and the rewrite is clearly helpful.

## Public Repository Safety

Only publish material that belongs in the public project. Do not commit:

- generated media bundles or local run output;
- local caches, virtual environments, or editor files;
- raw model-review prompts or reports;
- private operating notes or agent instructions;
- credentials, tokens, API keys, or private media.

Run `make public-check` when touching docs, workflows, fixtures, or repository
metadata. CI also runs this check through `make verify`.

## Fixtures

Fixtures must be synthetic or otherwise explicitly redistributable. Do not add
copyrighted media, downloaded talk audio, private recordings, or third-party
transcripts as test fixtures.

Unit tests must not require network access. Mark network-dependent tests as
`integration` so they stay out of the default verification path.

## Privacy

Lectern is local-first. Local-source media and derived artifacts must not be sent
to remote services unless the user explicitly opts in for that item and stage.
