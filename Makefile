.PHONY: db migrate test zone-evals evals prove-naive run staff
db:            ## start a local Postgres 16 for development and tests
	@scripts/local_postgres.sh start
migrate:       ## apply roles and migrations (needs ADMIN_DATABASE_URL)
	python -m app.migrate
test:          ## full test suite (needs ADMIN_DATABASE_URL); no model calls
	python -m pytest -q
zone-evals:    ## zone classifier precision/recall with the real model
	python -m evals.run_zone_evals --samples 3
evals:         ## behavior evals against the production pipeline
	python -m evals.run_evals --samples 3
prove-naive:   ## show the naive baseline fails every case
	python -m evals.run_evals --naive --expect fail
run:
	uvicorn app.api:app --reload
