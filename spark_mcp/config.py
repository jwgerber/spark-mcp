"""Configuration for Spark MCP Server."""

import re
from pathlib import Path
import json
from typing import Optional, Iterable


CONFIG_FILE = Path.home() / ".mcp-config" / "spark" / "config.json"

# Default configuration
DEFAULTS = {
    "signature_image_path": str(Path.home() / "Documents/letter-template/sig.png"),
    "pdf_output_dir": str(Path.home() / "Downloads"),
    # Directories the PDF tools are allowed to read from / write to. Any
    # filePath / outputPath / signatureImagePath supplied by the caller must
    # resolve to a location inside one of these roots. This is the primary
    # defense against prompt-injection-driven arbitrary file read/write.
    "allowed_pdf_roots": [
        str(Path.home() / "Downloads"),
        str(Path.home() / "Documents"),
        str(Path.home() / "Desktop"),
    ],
}


# ---------------------------------------------------------------------------
# Path sandboxing
# ---------------------------------------------------------------------------

class UnsafePathError(ValueError):
    """Raised when a caller-supplied path escapes the configured sandbox."""


def _allowed_roots() -> list[Path]:
    config = load_config()
    roots = config.get("allowed_pdf_roots") or DEFAULTS["allowed_pdf_roots"]
    # Also permit the signature and templates dirs implicitly
    extra = [
        Path(config.get("signature_image_path", "")).expanduser().parent,
        get_templates_dir(),
    ]
    resolved: list[Path] = []
    for r in list(roots) + extra:
        try:
            p = Path(str(r)).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if str(p) and p not in resolved:
            resolved.append(p)
    return resolved


def resolve_safe_path(
    user_path: str,
    *,
    must_exist: bool = False,
    require_suffix: Optional[Iterable[str]] = None,
) -> Path:
    """Resolve a caller-supplied path and enforce sandbox containment.

    - Expands ``~`` and resolves symlinks / ``..``
    - Rejects the path if it does not resolve under any allowed root
    - Optionally requires the path to exist
    - Optionally requires a specific file extension (case-insensitive)

    Raises ``UnsafePathError`` on any violation. This is the only function
    callers should use to translate LLM-supplied paths into filesystem ops.
    """
    if not user_path or not isinstance(user_path, str):
        raise UnsafePathError("Path must be a non-empty string")

    expanded = Path(user_path).expanduser()
    # For non-existing outputs we still want to resolve .. segments; use
    # strict=False so a missing leaf doesn't raise.
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Could not resolve path: {e}") from e

    roots = _allowed_roots()
    if not any(_is_within(resolved, root) for root in roots):
        raise UnsafePathError(
            "Path is outside the allowed directories. "
            f"Allowed roots: {[str(r) for r in roots]}"
        )

    if require_suffix is not None:
        allowed = {s.lower() for s in require_suffix}
        if resolved.suffix.lower() not in allowed:
            raise UnsafePathError(
                f"Path must have one of these extensions: {sorted(allowed)}"
            )

    if must_exist and not resolved.exists():
        raise UnsafePathError("Path does not exist")

    return resolved


def _is_within(candidate: Path, root: Path) -> bool:
    """True if ``candidate`` is equal to or inside ``root`` after resolution."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def validate_template_name(name: str) -> str:
    """Validate a template name to prevent path traversal via filenames."""
    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.match(name):
        raise UnsafePathError(
            "Template name must match [A-Za-z0-9_-] and be 1-64 characters"
        )
    return name


def load_config() -> dict:
    """Load configuration from file, with defaults."""
    config = DEFAULTS.copy()

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                user_config = json.load(f)
                config.update(user_config)
        except (json.JSONDecodeError, IOError):
            pass

    return config


def save_config(config: dict) -> None:
    """Save configuration to file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def get_signature_path() -> Optional[str]:
    """Get the configured signature image path."""
    config = load_config()
    path = Path(config.get("signature_image_path", "")).expanduser()
    return str(path) if path.exists() else None


def get_output_dir() -> str:
    """Get the configured PDF output directory."""
    config = load_config()
    return config.get("pdf_output_dir", str(Path.home() / "Downloads"))


def get_templates_dir() -> Path:
    """Get the directory for PDF templates."""
    templates_dir = CONFIG_FILE.parent / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return templates_dir


def save_template(template_name: str, template_data: dict) -> Path:
    """Save a PDF template to the templates directory."""
    validate_template_name(template_name)
    templates_dir = get_templates_dir()
    template_path = templates_dir / f"{template_name}.json"
    with open(template_path, 'w') as f:
        json.dump(template_data, f, indent=2)
    return template_path


def load_template(template_name: str) -> Optional[dict]:
    """Load a PDF template from the templates directory."""
    validate_template_name(template_name)
    templates_dir = get_templates_dir()
    template_path = templates_dir / f"{template_name}.json"
    if not template_path.exists():
        return None
    try:
        with open(template_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def list_templates() -> list:
    """List all available PDF templates."""
    templates_dir = get_templates_dir()
    templates = []
    for template_path in templates_dir.glob("*.json"):
        try:
            with open(template_path) as f:
                data = json.load(f)
                templates.append({
                    "name": template_path.stem,
                    "fields": len(data.get("fields", [])),
                    "description": data.get("description", "")
                })
        except (json.JSONDecodeError, IOError):
            continue
    return templates


def delete_template(template_name: str) -> bool:
    """Delete a PDF template."""
    validate_template_name(template_name)
    templates_dir = get_templates_dir()
    template_path = templates_dir / f"{template_name}.json"
    if template_path.exists():
        template_path.unlink()
        return True
    return False
