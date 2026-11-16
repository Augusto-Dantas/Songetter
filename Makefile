VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PIP ?= $(VENV)/bin/pip
PYTEST ?= $(VENV)/bin/pytest
STREAMLIT ?= $(VENV)/bin/streamlit
HOST ?= 0.0.0.0
PORT ?= 8000
LIBRARY_DIR ?= ./Music

.PHONY: install tui serve streamlit doctor test docker-build android-apk

install:
	@if [ ! -x "$(VENV)/bin/python" ]; then \
		python3 -m venv "$(VENV)"; \
	fi
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

tui:
	$(PYTHON) -m songetter.cli tui -o "$(LIBRARY_DIR)"

serve:
	$(PYTHON) -m songetter.cli serve --host "$(HOST)" --port "$(PORT)" -o "$(LIBRARY_DIR)"

streamlit:
	$(STREAMLIT) run app.py

doctor:
	$(PYTHON) -m songetter.cli doctor -o "$(LIBRARY_DIR)"

test:
	$(PYTEST) -q

docker-build:
	docker build -t songetter:latest .

android-apk:
	@if [ -n "$(ANDROID_HOME)$(ANDROID_SDK_ROOT)" ] && [ -x android/gradlew ]; then \
		cd android && ./gradlew assembleDebug --no-daemon; \
	elif [ -n "$(ANDROID_HOME)$(ANDROID_SDK_ROOT)" ] && command -v gradle >/dev/null 2>&1; then \
		cd android && gradle assembleDebug --no-daemon; \
	else \
		docker build -f Dockerfile.android -t songetter-android:latest .; \
	fi
