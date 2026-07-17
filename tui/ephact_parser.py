"""
Ephemeral Artifact (ephact) parser.

Detects <ephact type="...">...</ephact> tags in agent speech.
Returns cleaned text (tags removed) and extracted ephact objects.

Handles streaming: partial/unclosed tags are left in text until complete.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class EphactData:
    """A single extracted ephemeral artifact."""
    type: str       # table, list, code, paragraph
    content: str    # raw content between tags
    title: Optional[str] = None  # optional title attribute


# Matches complete <ephact type="...">...</ephact> blocks.
# Attributes: type (required), title (optional).
_EPHACT_RE = re.compile(
    r'<ephact\s+'
    r'type=["\'](\w+)["\']'        # type attribute (required)
    r'(?:\s+title=["\']([^"\']*)["\'])?'  # title attribute (optional)
    r'\s*>'
    r'(.*?)'                       # content (non-greedy)
    r'</ephact>',
    re.DOTALL,
)


_CODE_BLOCK_RE = re.compile(r'```.*?```|`[^`]+`', re.DOTALL)


def extract_ephacts(text: str) -> tuple[str, list[EphactData]]:
    """
    Extract all complete ephact blocks from text.

    Returns:
        (cleaned_text, ephacts) — text with tags removed, list of extracted artifacts.
        Partial/unclosed tags are left in text.
        Tags inside code blocks (``` or inline `) are ignored.
    """
    # Mask code blocks so ephact tags inside them aren't matched
    masks = []
    def _mask(m: re.Match) -> str:
        masks.append(m.group(0))
        return f"\x00MASK{len(masks)-1}\x00"
    masked = _CODE_BLOCK_RE.sub(_mask, text)

    ephacts = []
    def _collect(m: re.Match) -> str:
        etype = m.group(1)
        title = m.group(2)  # None if not present
        content = m.group(3).strip()
        ephacts.append(EphactData(type=etype, content=content, title=title))
        return ""  # strip from text

    cleaned = _EPHACT_RE.sub(_collect, masked)
    # Clean up extra blank lines left by removal
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    # Restore masked code blocks
    for i, original in enumerate(masks):
        cleaned = cleaned.replace(f"\x00MASK{i}\x00", original)
    return cleaned, ephacts


def has_partial_ephact(text: str) -> bool:
    """Check if text has an opening <ephact> tag without a closing </ephact>."""
    # Find all opening tags
    opens = list(re.finditer(r'<ephact\s+', text))
    if not opens:
        return False
    # Check if the last opening tag has a matching close
    last_open = opens[-1]
    remainder = text[last_open.start():]
    return '</ephact>' not in remainder
