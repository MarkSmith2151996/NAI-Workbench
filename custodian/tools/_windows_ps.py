from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from custodian.services.windows_bridge import run_command


_CLIXML_PREFIX = "#< CLIXML"


def _decode_clixml_text(text: str) -> str:
    text = text.replace("_x000D__x000A_", "\n")
    text = text.replace("_x000D_", "\r")
    text = text.replace("_x000A_", "\n")
    return text.strip()


def _strip_clixml(stderr: str) -> str:
    if not stderr:
        return ""

    stripped = stderr.strip()
    if not stripped.startswith(_CLIXML_PREFIX):
        return stderr

    xml_text = stripped[len(_CLIXML_PREFIX) :].strip()
    if not xml_text:
        return ""

    try:
        root = ET.fromstring(xml_text)
        messages: list[str] = []
        for node in root.iter():
            if node.text and node.tag.endswith("S"):
                decoded = _decode_clixml_text(node.text)
                if decoded:
                    messages.append(decoded)
        if messages:
            return "\n".join(dict.fromkeys(messages))
    except ET.ParseError:
        pass

    cleaned = re.sub(r"<[^>]+>", " ", xml_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _decode_clixml_text(cleaned)


def run_ps(command: str, timeout: int = 120) -> dict[str, str | int]:
    result = run_command(command=command, timeout=timeout)
    if not isinstance(result, dict):
        raise RuntimeError(f"Windows bridge returned unexpected result: {result!r}")

    if "error" in result:
        raise RuntimeError(_strip_clixml(str(result["error"])) or str(result["error"]))

    return {
        "stdout": str(result.get("stdout") or ""),
        "stderr": _strip_clixml(str(result.get("stderr") or "")),
        "exit_code": int(result.get("exit_code") or 0),
    }
