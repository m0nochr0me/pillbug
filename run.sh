#!/bin/bash

set -euo pipefail

exec uv run pillbug "$@"
