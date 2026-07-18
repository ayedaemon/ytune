COMPOSE = docker compose

.DEFAULT_GOAL := help

.PHONY: help up down build restart logs \
        db-shell db-status migrate extract-auth check-auth \
        download-clap-model \
        test test-unit test-integration \
        clean nuke

help: ## Show this help (API: see http://localhost:8000/docs)
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Setup

up: ## Start all services (build if needed)
	$(COMPOSE) up --build -d

down: ## Stop and remove containers
	$(COMPOSE) down

build: ## Rebuild all images
	$(COMPOSE) build

restart: down up ## Rebuild + restart everything

extract-auth: ## Extract YTM cookies from browser -> auth/ytmusic.json
	uv run scripts/extract_auth.py --browser firefox

check-auth: ## Validate extracted auth credentials
	uv run scripts/extract_auth.py --check

download-clap-model: ## Re-fetch the CLAP checkpoint manually (already baked in at image build time)
	$(COMPOSE) run --rm embeddings-clap python -c "\
import laion_clap, os; \
os.makedirs('/models/clap', exist_ok=True); \
m = laion_clap.CLAP_Module(enable_fusion=False); \
m.load_ckpt(); \
import shutil, glob; \
src = glob.glob(os.path.expanduser('~/.cache/laion_clap/*.pt'))[0]; \
shutil.copy(src, '/models/clap/clap.pt'); \
print('CLAP checkpoint ready at /models/clap/clap.pt')"

##@ Logs
logs: ## Tail logs — all services, or one: make logs SERVICE=ytm-sync
	$(COMPOSE) logs -f $(SERVICE)

##@ Database

db-shell: ## psql into ytmusic_db
	$(COMPOSE) exec db psql -U ytmusic -d ytmusic_db


migrate: ## Run pending migration files manually
	@echo "Running migrations..."
	@for f in db/migrations/*.sql; do \
		echo "  -> $$f"; \
		$(COMPOSE) exec -T db psql -U ytmusic -d ytmusic_db -f /dev/stdin < $$f; \
	done
	@echo "Done."


##@ Cleanup

clean: ## Remove containers (keep volumes)
	$(COMPOSE) down --remove-orphans

nuke: ## Remove containers AND volumes (wipes DB + storage)
	@echo "WARNING: This deletes all volumes (DB data, downloaded tracks, model cache)."
	@read -p "Type 'yes' to confirm: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted." && exit 1)
	$(COMPOSE) down -v --remove-orphans
