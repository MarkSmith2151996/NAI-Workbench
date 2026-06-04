from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from custodian.agents.schema import AgentSpec, validate_agent_spec


def load_spec(spec_data: dict[str, Any] | str | Path) -> AgentSpec:
    if isinstance(spec_data, Path):
        data = yaml.safe_load(spec_data.read_text(encoding="utf-8")) or {}
    elif isinstance(spec_data, str):
        path = Path(spec_data)
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            data = yaml.safe_load(spec_data) or {}
    else:
        data = spec_data
    return validate_agent_spec(data)
