.PHONY: test ingest mcp demo
test:
	python3 -m unittest discover -s tests -v
ingest:
	python3 -m grantlib ingest
mcp:
	python3 -m grantlib.mcp_server
demo:
	python3 scripts/demo.py
