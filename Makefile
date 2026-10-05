.PHONY: install dev test test-py test-js e2e check

install:
	pip install -r coordinator/requirements.txt

dev:
	cd coordinator && DJANGO_DEBUG=1 python3 manage.py migrate && DJANGO_DEBUG=1 python3 manage.py runserver

test: test-py test-js e2e

test-py:
	cd coordinator && DJANGO_DEBUG=1 LOG_LEVEL=ERROR python3 manage.py test

test-js:
	cd web && node --test test/*.test.js

e2e:
	scripts/run-e2e.sh

check:
	cd coordinator && DJANGO_DEBUG=1 python3 manage.py check && DJANGO_DEBUG=1 python3 manage.py makemigrations --check --dry-run
