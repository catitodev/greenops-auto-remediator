# =============================================================================
# GreenOps Auto-Remediador — Makefile
# =============================================================================
# Uso: make <comando>
# Exemplo: make setup && make test
# =============================================================================

# Configurações gerais
PYTHON        := python3.12
PIP           := pip
SRC_DIR       := src
TESTS_DIR     := tests
DOCS_DIR      := docs
SCRIPTS_DIR   := scripts
VENV_DIR      := .venv

# Ambiente de deploy (pode ser sobrescrito: make deploy ENV=staging)
ENV           ?= dev

# Região AWS (pode ser sobrescrita: make deploy AWS_REGION=sa-east-1)
AWS_REGION    ?= us-east-1

# Stack CloudFormation
STACK_NAME    := greenops-auto-remediator-$(ENV)
TEMPLATE_FILE := infrastructure/cloudformation/main.yaml

# Cores para output legível no terminal
CYAN  := \033[0;36m
RESET := \033[0m

.PHONY: help setup test lint format deploy clean docs

# -----------------------------------------------------------------------------
# help — lista todos os comandos disponíveis (comando padrão)
# -----------------------------------------------------------------------------
help:
	@echo ""
	@echo "$(CYAN)GreenOps Auto-Remediador$(RESET)"
	@echo "========================"
	@echo ""
	@echo "Comandos disponíveis:"
	@echo "  make setup    — Cria virtualenv e instala todas as dependências"
	@echo "  make test     — Executa todos os testes com cobertura"
	@echo "  make lint     — Verifica qualidade e segurança do código"
	@echo "  make format   — Formata o código com black e isort"
	@echo "  make deploy   — Faz deploy do CloudFormation na AWS (ENV=dev por padrão)"
	@echo "  make clean    — Remove artefatos temporários e cache"
	@echo "  make docs     — Gera e serve a documentação localmente"
	@echo ""
	@echo "Variáveis configuráveis:"
	@echo "  ENV=$(ENV)  AWS_REGION=$(AWS_REGION)"
	@echo ""

# -----------------------------------------------------------------------------
# setup — cria o ambiente virtual e instala dependências
# -----------------------------------------------------------------------------
setup:
	@echo "$(CYAN)→ Criando ambiente virtual em $(VENV_DIR)/$(RESET)"
	$(PYTHON) -m venv $(VENV_DIR)
	@echo "$(CYAN)→ Instalando dependências de produção$(RESET)"
	$(VENV_DIR)/bin/$(PIP) install --upgrade pip
	$(VENV_DIR)/bin/$(PIP) install -r requirements.txt
	@echo "$(CYAN)→ Instalando dependências de desenvolvimento$(RESET)"
	$(VENV_DIR)/bin/$(PIP) install -r requirements-dev.txt
	@echo "$(CYAN)→ Copiando .env.example para .env (se não existir)$(RESET)"
	@test -f .env || cp .env.example .env
	@echo ""
	@echo "✅ Setup concluído. Ative o venv com: source $(VENV_DIR)/bin/activate"

# -----------------------------------------------------------------------------
# test — executa todos os testes com relatório de cobertura
# -----------------------------------------------------------------------------
test:
	@echo "$(CYAN)→ Executando testes unitários$(RESET)"
	$(VENV_DIR)/bin/pytest $(TESTS_DIR)/unit \
		--cov=$(SRC_DIR) \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		--cov-fail-under=80 \
		-v
	@echo ""
	@echo "$(CYAN)→ Executando testes de integração$(RESET)"
	$(VENV_DIR)/bin/pytest $(TESTS_DIR)/integration -v
	@echo ""
	@echo "✅ Relatório de cobertura gerado em htmlcov/index.html"

# -----------------------------------------------------------------------------
# lint — verifica estilo, bugs e vulnerabilidades de segurança
# -----------------------------------------------------------------------------
lint:
	@echo "$(CYAN)→ flake8 — verificando estilo PEP8$(RESET)"
	$(VENV_DIR)/bin/flake8 $(SRC_DIR) --max-line-length=100 --statistics
	@echo ""
	@echo "$(CYAN)→ pylint — análise estática avançada$(RESET)"
	$(VENV_DIR)/bin/pylint $(SRC_DIR) --fail-under=8.0
	@echo ""
	@echo "$(CYAN)→ mypy — verificação de tipos$(RESET)"
	$(VENV_DIR)/bin/mypy $(SRC_DIR) --ignore-missing-imports
	@echo ""
	@echo "$(CYAN)→ bandit — análise de segurança$(RESET)"
	$(VENV_DIR)/bin/bandit -r $(SRC_DIR) -ll
	@echo ""
	@echo "✅ Lint concluído"

# -----------------------------------------------------------------------------
# format — formata o código automaticamente
# -----------------------------------------------------------------------------
format:
	@echo "$(CYAN)→ isort — organizando imports$(RESET)"
	$(VENV_DIR)/bin/isort $(SRC_DIR) $(TESTS_DIR)
	@echo ""
	@echo "$(CYAN)→ black — formatando código$(RESET)"
	$(VENV_DIR)/bin/black $(SRC_DIR) $(TESTS_DIR) --line-length=100
	@echo ""
	@echo "✅ Formatação concluída"

# -----------------------------------------------------------------------------
# deploy — valida e faz deploy do template CloudFormation
# Uso: make deploy ENV=staging AWS_REGION=sa-east-1
# -----------------------------------------------------------------------------
deploy:
	@echo "$(CYAN)→ Validando template CloudFormation$(RESET)"
	aws cloudformation validate-template \
		--template-body file://$(TEMPLATE_FILE) \
		--region $(AWS_REGION)
	@echo ""
	@echo "$(CYAN)→ Fazendo deploy da stack $(STACK_NAME) em $(AWS_REGION)$(RESET)"
	aws cloudformation deploy \
		--template-file $(TEMPLATE_FILE) \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION) \
		--capabilities CAPABILITY_NAMED_IAM \
		--parameter-overrides \
			Environment=$(ENV) \
		--tags \
			GreenOpsManaged=true \
			Environment=$(ENV) \
			Project=greenops-auto-remediator
	@echo ""
	@echo "✅ Deploy concluído. Stack: $(STACK_NAME)"

# -----------------------------------------------------------------------------
# clean — remove artefatos temporários, cache e arquivos gerados
# -----------------------------------------------------------------------------
clean:
	@echo "$(CYAN)→ Removendo cache Python$(RESET)"
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "$(CYAN)→ Removendo artefatos de teste$(RESET)"
	rm -rf .pytest_cache htmlcov .coverage coverage.xml
	@echo "$(CYAN)→ Removendo artefatos de build$(RESET)"
	rm -rf dist build .mypy_cache
	@echo ""
	@echo "✅ Limpeza concluída"

# -----------------------------------------------------------------------------
# docs — gera e serve a documentação localmente em http://localhost:8000
# -----------------------------------------------------------------------------
docs:
	@echo "$(CYAN)→ Servindo documentação em http://localhost:8000$(RESET)"
	@echo "   Pressione Ctrl+C para parar"
	$(VENV_DIR)/bin/mkdocs serve --dev-addr=0.0.0.0:8000
