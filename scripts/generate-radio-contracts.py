"""Generate radio MCP input schemas from the runtime registry (no credentials)."""
import json
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'apps/backend/src'))
from archive_backend.contracts import CAPABILITIES
from archive_backend.radio_contracts import tool_schema
value={'schemaVersion':1,'methods':{name:tool_schema(cap) for name,cap in CAPABILITIES.items() if name.startswith('radio.')}}
(root/'contracts/radio/mcp-input-schemas.json').write_text(json.dumps(value,indent=2)+'\n')
