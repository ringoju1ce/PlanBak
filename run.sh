#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$project_dir/.venv/bin/planbak" ]]; then
    printf '%s\n' '请先在项目目录安装依赖：uv sync --extra dev' >&2
    exit 1
fi
exec "$project_dir/.venv/bin/planbak" "$@"
