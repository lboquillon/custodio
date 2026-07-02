# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
#
# One-command workflows. `uv` manages an isolated virtualenv and, if needed,
# downloads the pinned Python interpreter — so the host only needs `uv`.
#   https://docs.astral.sh/uv/

VENV        := .venv
BIN         := $(VENV)/bin
PYVER       ?= 3.12
PORT        ?= 3000
# Best recall out of the standard spaCy models (~560MB). Override for a smaller
# footprint (en_core_web_sm) or maximum accuracy (en_core_web_trf, see make run-max).
SPACY_MODEL ?= en_core_web_lg
UVPY        := --python $(VENV)

.DEFAULT_GOAL := help

.PHONY: help run run-max run-regex install install-full install-trf test lint docker docker-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Quick start:  make run        (full spaCy NER, en_core_web_lg — recommended)"
	@echo "                make run-max    (transformer model, highest accuracy, heavy)"
	@echo "                make run-regex  (no spaCy; faster, lower recall)"
	@echo "                make docker     (production container + Redis)"

$(VENV):
	uv venv --python $(PYVER) $(VENV)

install: $(VENV) ## Install base deps (enables the regex engine)
	uv pip install $(UVPY) -e .

install-full: $(VENV) ## Install full NER deps + spaCy model
	uv pip install $(UVPY) -e ".[full]"
	@$(BIN)/python -c "import $(SPACY_MODEL)" 2>/dev/null \
	  || $(BIN)/python -m spacy download $(SPACY_MODEL)

run: install-full ## Run with full spaCy NER detection (recommended)
	CUSTODIO_ENGINE=presidio CUSTODIO_SPACY_MODEL=$(SPACY_MODEL) \
	  $(BIN)/custodio serve --port $(PORT)

install-trf: $(VENV) ## Install NER + transformer stack + en_core_web_trf model
	uv pip install $(UVPY) -e ".[full,transformers]"
	@$(BIN)/python -c "import en_core_web_trf" 2>/dev/null \
	  || $(BIN)/python -m spacy download en_core_web_trf

run-max: install-trf ## Run with the transformer model (highest accuracy, heavy)
	CUSTODIO_ENGINE=presidio CUSTODIO_SPACY_MODEL=en_core_web_trf \
	  $(BIN)/custodio serve --port $(PORT)

run-regex: install ## Run with the regex engine (no spaCy; faster, lower recall)
	CUSTODIO_ENGINE=regex $(BIN)/custodio serve --engine regex --port $(PORT)

test: install ## Run the test suite
	uv pip install $(UVPY) -e ".[dev,full]"
	$(BIN)/python -m pytest

lint: ## Lint with ruff
	uvx ruff check custodio tests

docker: ## Build and run the production container
	docker compose up --build

docker-down: ## Stop the container
	docker compose down

clean: ## Remove the virtualenv and caches
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__ *.egg-info
