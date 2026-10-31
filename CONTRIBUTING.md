# Contributing

Grant Tool's product interface is stdio MCP. The frontier-model assistant is the caller, reasoner, writer, and reviewer. The server retrieves and preserves state without model inference. Contributions must preserve that separation; do not add a UI, website, HTTP service, or nested-model tool.

Focused improvements to source handling, scientific workflows, and reliability are welcome. Describe the problem with invented inputs and explain resulting behavior and validation.

Use Python 3.11 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
python -m unittest discover -s tests -v
python scripts/demo.py
python scripts/check_public_release.py --history
```

Some extraction tests need optional system utilities; inspect skipped-test reasons. Tests use synthetic sources, isolated stores, and mocked services. Live model evaluation is separate: read [evaluation limits](docs/ASSISTANT_DEVELOPMENT.md) and [MCP comparative evaluation](docs/MCP_EVALUATION.md) before running it. Mocked workflows are software evidence, not measured scientific quality.

Preserve source ownership, proposed/completed status, explicit parents, optimistic revision checks, and historical evidence. Add tests for affected behavior, including no-model execution, response bounds, source scope, stale revisions, and cross-proposal isolation. Do not turn an exact quotation or valid citation identifier into an automatic support judgment.

First-use setup must use only supplied folders and funding-call inputs. Keep unknown details explicit. Memory tools may propose records and select already-confirmed versions; a model argument cannot authenticate the investigator.

All committed examples must be invented. Follow [data handling](PRIVACY.md) before attaching logs or files. Contributions use the [MIT license](LICENSE). Public-source documents and dependencies retain their own terms; this license does not relicense research content.
