#!/bin/sh
set -eu
python3 -m compileall -q metrology
python3 -m unittest discover -s tests -v
python3 -m metrology demo
