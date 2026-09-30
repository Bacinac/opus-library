"""Subtitle policy: the acceptance test of every import. A video item is
complete only when its files satisfy the policy — 'any' needs at least one of
the wanted languages, 'all' needs every one, 'none' waives the requirement.
Per-item overrides use the same 'mode:lang,lang' syntax."""

import re
from dataclasses import dataclass

OVERRIDE_RE = re.compile(r"^(none|(any|all):[a-z]{2,3}(,[a-z]{2,3})*)$")


@dataclass(frozen=True)
class SubtitlePolicy:
    mode: str  # any | all | none
    langs: tuple[str, ...]


@dataclass(frozen=True)
class PolicyResult:
    satisfied: bool
    missing: tuple[str, ...]
    present: tuple[str, ...]


def normal_override(spec: str | None) -> str:
    return (spec or "").strip().lower()


def parse_override(spec: str) -> SubtitlePolicy | None:
    """Parses an item override like 'all:en,hr' or 'none'; empty/invalid →
    None (inherit global)."""
    spec = normal_override(spec)
    if not spec or not OVERRIDE_RE.match(spec):
        return None
    mode, _, langs = spec.partition(":")
    return SubtitlePolicy(mode, tuple(p for p in langs.split(",") if p))


def effective_policy(config, override: str = "") -> SubtitlePolicy:
    policy = parse_override(override)
    if policy is not None:
        return policy
    return SubtitlePolicy(config.get("subtitle_mode"), tuple(config.langs()))


def evaluate(policy: SubtitlePolicy, subtitles: list[dict], *,
             accept_auto: bool = False) -> PolicyResult:
    """subtitles: [{lang, auto_generated}]-shaped dicts or ORM rows."""
    present = set()
    for s in subtitles:
        lang = s["lang"] if isinstance(s, dict) else s.lang
        auto = s.get("auto_generated", False) if isinstance(s, dict) else s.auto_generated
        if auto and not accept_auto:
            continue
        present.add(lang)

    wanted = set(policy.langs)
    if policy.mode == "none" or not wanted:
        return PolicyResult(True, (), tuple(sorted(present)))
    if policy.mode == "any":
        satisfied = bool(wanted & present)
        missing = () if satisfied else tuple(sorted(wanted - present))
    else:  # all
        missing = tuple(sorted(wanted - present))
        satisfied = not missing
    return PolicyResult(satisfied, missing, tuple(sorted(present & wanted)))


def policy_met(config, override: str, langs) -> bool:
    """Whether an item whose files carry these languages is complete. The
    languages are already the ones that count: auto-generated tracks left out
    unless the install accepts them."""
    return evaluate(effective_policy(config, override),
                    [{"lang": lang} for lang in langs or ()]).satisfied
