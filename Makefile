.PHONY: help install test lint format e2e demo clean run doctor

help:
	@echo "install   install AgentLite with all extras (editable)"
	@echo "test      run the test suite"
	@echo "lint      ruff check"
	@echo "format    ruff format"
	@echo "demo      run the end-to-end demo against the fake model server"
	@echo "clean     remove caches and build artefacts"

install:
	python -m venv .venv && . .venv/bin/activate && pip install -e ".[browser,dev]"

test:
	pytest

lint:
	ruff check agentlite tests examples

format:
	ruff format agentlite tests examples

# Keyless end-to-end check: fake model in one process, AgentLite in another.
demo:
	python examples/fake_llm_server.py --port 8100 & \
		sleep 2; \
		AGENTLITE_BASE_URL=http://127.0.0.1:8100/v1 AGENTLITE_API_KEY=demo-key \
		AGENTLITE_PROVIDER=openai-compatible \
		agentlite run "inspect the workspace and write a report"; \
		kill %1

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
