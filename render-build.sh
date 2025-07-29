#!/usr/bin/env bash
set -o errexit

python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
