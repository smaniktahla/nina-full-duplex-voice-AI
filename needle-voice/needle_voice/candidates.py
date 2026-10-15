"""Deterministic target retrieval. Pure, stdlib only.

Given the utterance and the exposed-entity snapshot, rank the entities/areas
the user could plausibly mean. This does two jobs:

  1. It gives Qwen a SHORT candidate list (a full exposed-entity list is
     ~35K tokens; a fast model can't take that and stay under ~1s).
  2. It is the authority on which target is meant. Qwen's chosen target must
     equal the unique top-ranked candidate, otherwise the request escalates.
     The model proposes an intent; it does not get to pick a different device.

Scoring is an F1 of (a) how much of the entity/area name the user said and
(b) how much of what the user said (minus verbs/filler) the name accounts for.
That is what separates "Pool Fan" from "Swimming Pool Exhaust Fan" when the
user says "pool exhaust fan". Exact-token matching only after simple plural
stripping: no substring or edit-distance matching.
"""

from __future__ import annotations

from dataclasses import dataclass

from .guards import tokens

# words that carry no target identity
FILLER = frozenset({
    "the", "a", "an", "please", "my", "our", "in", "to", "of", "for", "is", "are", "it", "its",
    "turn", "switch", "power", "put", "flip", "set", "make", "change", "toggle",
    "on", "off", "up", "down", "enable", "disable", "activate", "deactivate", "start", "stop",
    "open", "close", "lock", "unlock", "raise", "shut", "run", "execute", "trigger", "launch",
    "play", "pause", "resume", "unpause", "hold", "dim", "brighten", "brightness", "percent",
    "what", "whats", "status", "state", "check", "tell", "me", "can", "you", "could", "now", "today", "currently",
    "at", "degrees", "degree", "celsius", "fahrenheit",
})

# nouns that name a device TYPE rather than a specific device
DOMAIN_NOUNS = {
    "light": "light", "lamp": "light", "fan": "fan", "switch": "switch", "tv": "media_player",
    "television": "media_player", "speaker": "media_player", "thermostat": "climate",
    "sensor": "sensor", "door": "cover", "garage": "cover", "blind": "cover", "shade": "cover",
}
# Words that name a device TYPE and so never identify a specific device on their
# own. Kept separate from DOMAIN_NOUNS on purpose: "garage" hints at covers but is
# also an area/name word ("Garage Lamp") and must be allowed to identify.
GENERIC = frozenset({"light", "lamp", "fan", "switch", "sensor", "room", "area", "tv", "television", "speaker", "thermostat"})

MIN_SCORE = 0.5
UNIQUE_MARGIN = 0.08
MAX_CANDIDATES = 8
KEEP_RATIO = 0.7


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def _stems(text: str) -> list[str]:
    return [_stem(t) for t in tokens(text)]


@dataclass(frozen=True)
class Candidate:
    id: str            # entity_id, or "area:<area_id>"
    kind: str          # "entity" | "area"
    name: str
    domain: str | None
    area: str | None
    score: float


def _phrases(item: dict) -> list[list[str]]:
    out = []
    names = [item.get("name")] + [a for a in (item.get("aliases") or []) if isinstance(a, str)]
    for n in names:
        if isinstance(n, str):
            st = [s for s in _stems(n) if s]
            if st:
                out.append(st)
    return out


def _f1(name_stems: list[str], content: set[str], utt: set[str]) -> float:
    name_set = set(name_stems)
    matched = name_set & utt
    if not matched or not (matched - GENERIC):
        return 0.0  # only generic words ("light", "fan") matched: not identifying
    name_cov = len(matched) / len(name_set)
    utt_cov = len(matched & content) / len(content) if content else 0.0
    if name_cov == 0 or utt_cov == 0:
        return 0.0
    return 2 * name_cov * utt_cov / (name_cov + utt_cov)


def rank(utterance: str, snapshot: dict | None, allowed_domains: frozenset[str] | None = None) -> list[Candidate]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("entities"), list):
        return []
    utt = set(_stems(utterance))
    # digits are arguments (brightness, temperature), never part of a device name
    content = {s for s in utt if s not in FILLER and s not in GENERIC and not s.isdigit()}
    type_hints = {DOMAIN_NOUNS[s] for s in utt if s in DOMAIN_NOUNS}
    areas = {a.get("area_id"): a for a in snapshot.get("areas") or [] if isinstance(a, dict)}
    out: list[Candidate] = []

    for ent in snapshot["entities"]:
        if not isinstance(ent, dict) or not isinstance(ent.get("entity_id"), str):
            continue
        domain = ent["entity_id"].split(".")[0]
        if allowed_domains is not None and domain not in allowed_domains:
            continue
        best = max((_f1(p, content, utt) for p in _phrases(ent)), default=0.0)
        if best <= 0:
            continue
        if type_hints and domain in type_hints:
            best += 0.05
        elif type_hints and domain not in type_hints and "light" in type_hints:
            best -= 0.05
        if best >= MIN_SCORE:
            area = areas.get(ent.get("area_id"))
            out.append(Candidate(ent["entity_id"], "entity", str(ent.get("name") or ent["entity_id"]),
                                 domain, area.get("name") if area else None, round(best, 3)))

    for area in areas.values():
        best = max((_f1(p, content, utt) for p in _phrases(area)), default=0.0)
        if best >= MIN_SCORE:
            # an area is a group target: slightly below an exactly-named single entity
            out.append(Candidate(f"area:{area.get('area_id')}", "area", str(area.get("name")), None, None,
                                 round(best * 0.95, 3)))

    out.sort(key=lambda c: (-c.score, len(c.name), c.id))
    if out:
        # drop clutter: a fast model gets more hesitant the more look-alikes it is shown
        out = [x for x in out if x.score >= out[0].score * KEEP_RATIO]
    return out[:MAX_CANDIDATES]


def unique_top(cands: list[Candidate]) -> Candidate | None:
    """The single clearly-best candidate, or None if empty/ambiguous."""
    if not cands:
        return None
    if len(cands) == 1 or cands[0].score - cands[1].score >= UNIQUE_MARGIN:
        return cands[0]
    return None


def merge_lexicon(snapshot: dict | None, lexicon: dict | None) -> dict | None:
    """Add household-specific spoken names ({entity_id: [phrases]} and
    {"area:<area_id>": [phrases]}) as aliases. Unknown ids are ignored, so a
    stale lexicon entry can never create a target that isn't exposed."""
    if not isinstance(snapshot, dict) or not isinstance(lexicon, dict) or not lexicon:
        return snapshot
    ents = []
    for e in snapshot.get("entities") or []:
        extra = lexicon.get(e.get("entity_id")) if isinstance(e, dict) else None
        if isinstance(extra, list) and extra:
            e = {**e, "aliases": list(e.get("aliases") or []) + [x for x in extra if isinstance(x, str)]}
        ents.append(e)
    areas = []
    for a in snapshot.get("areas") or []:
        extra = lexicon.get(f"area:{a.get('area_id')}") if isinstance(a, dict) else None
        if isinstance(extra, list) and extra:
            a = {**a, "aliases": list(a.get("aliases") or []) + [x for x in extra if isinstance(x, str)]}
        areas.append(a)
    return {**snapshot, "entities": ents, "areas": areas}


def equivalent_candidates(top: "Candidate | None", snapshot: dict | None) -> list[Candidate]:
    """Targets that would have exactly the same effect as `top`. Today: if `top`
    is an area whose ONLY exposed light is one entity, that entity is equivalent
    (turning off "the kitchen lights" and turning off the kitchen's single light
    are the same action). A model naming one light of SEVERAL is not equivalent."""
    if top is None or top.kind != "area" or not isinstance(snapshot, dict):
        return []
    area_id = top.id[len("area:"):]
    lights = [e for e in snapshot.get("entities") or []
              if isinstance(e, dict) and e.get("area_id") == area_id and str(e.get("entity_id", "")).startswith("light.")]
    if len(lights) != 1:
        return []
    e = lights[0]
    return [Candidate(e["entity_id"], "entity", str(e.get("name") or e["entity_id"]), "light", top.name, top.score)]
