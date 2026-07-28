.PHONY: all format format-check lint typecheck test tests integration_tests help run dev production production-image production-check

# Default target executed when no arguments are given to make.
all: help

######################
# DEVELOPMENT
######################

PRODUCTION_PORT ?= 2024
COMPOSE ?= docker-compose

dev:
	uv run langgraph dev

# `langgraph up` is deliberately not used: langgraph-cli 0.4.30 paired with
# api 0.11.1 tags an image containing no project code and no entrypoint
# (CMD ["python3"]), then exits 0 while the api service silently no-ops.
# Building the CLI-generated Dockerfile with plain `docker build` produces a
# correct image; deploy/compose.yaml runs it.
production-image:
	uv run langgraph dockerfile -c langgraph.json deploy/api.Dockerfile
	docker build -f deploy/api.Dockerfile -t open-swe-control-plane-api:local .

production: production-image
	OPEN_SWE_API_PORT=$(PRODUCTION_PORT) OPEN_SWE_ENV_FILE=$(abspath .env) \
		$(COMPOSE) --project-name open-swe-control-plane -f deploy/compose.yaml up

production-check:
	uv run langgraph validate -c langgraph.json
	@output=$$(mktemp); \
		uv run langgraph dockerfile -c langgraph.json $$output; \
		rm -f $$output

run:
	uv run uvicorn agent.webapp:app --reload --port 8000

install:
	uv sync --extra dev

######################
# TESTING
######################

TEST_FILE ?= tests/

test tests:
	@if [ -d "$(TEST_FILE)" ] || [ -f "$(TEST_FILE)" ]; then \
		uv run pytest -vvv $(TEST_FILE); \
	else \
		echo "Skipping tests: path not found: $(TEST_FILE)"; \
	fi

integration_tests:
	@if [ -d "tests/integration_tests/" ] || [ -f "tests/integration_tests/" ]; then \
		uv run pytest -vvv tests/integration_tests/; \
	else \
		echo "Skipping integration tests: path not found: tests/integration_tests/"; \
	fi

######################
# LINTING AND FORMATTING
######################

PYTHON_FILES=.

lint:
	uv run ruff check $(PYTHON_FILES)
	uv run ruff format $(PYTHON_FILES) --diff

format:
	uv run ruff format $(PYTHON_FILES)
	uv run ruff check --fix $(PYTHON_FILES)

format-check:
	uv run ruff format $(PYTHON_FILES) --check

typecheck:
	npx --yes basedpyright agent tests

######################
# HELP
######################

help:
	@echo '----'
	@echo 'dev                          - run LangGraph dev server'
	@echo 'production                   - build image + run persistence-backed Agent Server via compose'
	@echo 'production-image             - render Dockerfile and docker-build the API image'
	@echo 'production-check             - validate and render production configuration'
	@echo 'run                          - run webhook server'
	@echo 'install                      - install dependencies (incl. dev extras)'
	@echo 'format                       - run code formatters'
	@echo 'lint                         - run linters'
	@echo 'typecheck                    - run basedpyright on agent/ and tests/'
	@echo 'test                         - run unit tests'
	@echo 'integration_tests            - run integration tests'
	@echo '----'
