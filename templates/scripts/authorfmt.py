#!/usr/bin/env python3
"""Shared author-list formatter used by BOTH channel pushers
(push_discord.py and push_telegram.py) so they render author lists
identically and can't drift. Lives in scripts/ — the shared folder
present in every deployment.
"""


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, drop punctuation."""
    n = name.lower().replace(",", " ").replace(".", " ").replace("-", " ")
    return " ".join(t for t in n.split() if t)


def _names_match(a: str, b: str) -> bool:
    """Two names likely refer to the same person if they share at least one
    token of length ≥2 (handles 2-letter Chinese surnames like Pu/Lu/He/Wu
    AND fuzzy surname matching like 'Gang Pei' vs 'Pei, G'). Single-letter
    initials are ignored to avoid spurious matches."""
    ta = {t for t in _normalize_name(a).split() if len(t) >= 2}
    tb = {t for t in _normalize_name(b).split() if len(t) >= 2}
    return bool(ta & tb)


def _format_authors_with_corresponding(authors: str, corresponding: str) -> str:
    """Compact author list: first 8 names, then '...', then a tail that always
    contains the corresponding-author group (CNS papers commonly have 4-8
    co-corresponding authors clustered at the end).

    `corresponding` may be a single name or a '; '-joined list of names."""
    if not authors:
        return corresponding or ""
    parts = [a.strip() for a in authors.split(";") if a.strip()]
    n = len(parts)
    corr_list = [c.strip() for c in (corresponding or "").split(";") if c.strip()]

    if n <= 12 and not corr_list:
        return "; ".join(parts)
    if n <= 12:
        # Short list: keep all, but if a corresponding name isn't already
        # represented (fuzzy match), append it.
        out = list(parts)
        for c in corr_list:
            if not any(_names_match(c, t) for t in parts):
                out.append(c)
        return "; ".join(out)

    head = parts[:8]
    # Tail strategy: walk the original list from the end, collect at least 2
    # names AND every name that fuzzy-matches a corresponding entry. This
    # captures the multi-PI corresponding cluster CNS papers love.
    tail_keep = set()
    # Always keep absolute last 2.
    for nm in parts[-2:]:
        tail_keep.add(nm)
    # Keep any author whose surname matches a corresponding entry.
    for nm in parts:
        if any(_names_match(nm, c) for c in corr_list):
            tail_keep.add(nm)
    # Re-order tail by their original position in `parts`.
    tail = [nm for nm in parts if nm in tail_keep]

    # If a corresponding name has NO author-list match (rare), splice it in
    # before the last author so the user still sees the explicit corr name.
    for c in corr_list:
        if not any(_names_match(c, t) for t in tail):
            tail = tail[:-1] + [c] + tail[-1:]

    # Cap tail to a reasonable length — if it ballooned we still want the
    # message readable. 6 is plenty for even mega-collab papers.
    if len(tail) > 6:
        tail = tail[-6:]

    return "; ".join(head) + "; ... ; " + "; ".join(tail)
