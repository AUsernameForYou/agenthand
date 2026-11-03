"""Scene guard (Dan's rule, 2026-11-03; tracker event 631): a draw
scene prompt describes the ENVIRONMENT only — the room, the surfaces,
the light. It must not name the product.

Mirror of the Rust build's `sceneguard` module: same matching, same
record shape, same refusal text, so a draw refused (or passed) here
reads identically in both builds.

The failure this prevents is measured, not hypothetical: when the
scene text named or framed the item being staged, the plate model
painted its own copy of the product into the scene beside the real
one (the library pickaxe draws v2/v3/v4 each invented a fake
duplicate pickaxe; three of four generated-plate draws for that
item). A staged photo carrying a second, invented product breaks
the Fidelity Lock's promise even though the real product's pixels
never changed, so the check belongs in the product, before spend.

`server.draw` runs `check_scene` against the foreground's cut
concept (the phrase `cut_out` recorded in the mask's PNG chunk)
before any plate is generated or edited — in fact before any paid
call of the draw. A scene that names the item is refused with
`SCENE_NAMES_PRODUCT`, the offending term, and the correction; the
refusal counts $0 and no ledger row is written. The outcome rides
on the persisted draw receipt and the tool result as the additive
`scene_guard` record, so provenance shows the check ran on every
draw.

The guard is deliberately narrow: it blocks the item's OWN name in
its OWN scene text — the concept's full phrase (as a token run) or
its head noun (as a whole word, or the head noun plus a trailing
"s"). It is not an object-word dictionary and not a model call;
other objects in the scene text (a table, a fence) are the caller's
business, exactly as before. When the foreground's mask carries no
concept (masks written before the chunk existed) there is nothing
honest to check against: the draw proceeds and the record says
`skipped`, with the reason.
"""

from __future__ import annotations

# The rule, stated once, carried in every guard record. Byte-identical
# to the Rust build's RULE.
RULE = (
    "the scene text describes the environment only — the room, the "
    "surfaces, the light — and must not name the product: naming it "
    "makes the image model paint its own copy of the product into the scene"
)

_SKIPPED_REASON = (
    "no concept recorded in the foreground's mask; "
    "nothing to check the scene text against"
)


def _word_tokens(text: str) -> list[str]:
    """Lowercased alphanumeric word tokens of `text` (anything else
    is a separator), in order."""
    tokens: list[str] = []
    current: list[str] = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _contains_token_run(haystack: list[str], needle: list[str]) -> bool:
    """True when `needle` appears in `haystack` as a contiguous run."""
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[i : i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )


class SceneGuard:
    """The outcome of one scene check."""

    def __init__(
        self,
        status: str,
        concept: str | None,
        checked_terms: list[str],
        offending_term: str | None = None,
    ) -> None:
        self.status = status  # "pass" | "refused" | "skipped"
        self.concept = concept
        self.checked_terms = checked_terms
        self.offending_term = offending_term

    @property
    def is_refusal(self) -> bool:
        return self.status == "refused"

    def refusal_message(self) -> str:
        """The refusal message: the offending term, the rule in one
        line, and the concrete correction — the house error style
        (every failure carries its next step, never a bare refusal)."""
        return (
            f'The scene text names the product: "{self.offending_term}" '
            f'(from the cut concept "{self.concept}"). Naming the product '
            "in a scene prompt makes the image model paint its own copy "
            "of it beside the real one. Describe the environment only — "
            "the room, the surfaces, the light — and call draw again."
        )

    def record(self) -> dict:
        """The `scene_guard` record persisted on the draw receipt and
        mirrored onto the tool result."""
        record = {
            "status": self.status,
            "concept": self.concept,
            "checked_terms": list(self.checked_terms),
            "rule": RULE,
        }
        if self.offending_term is not None:
            record["offending_term"] = self.offending_term
        if self.status == "skipped":
            record["reason"] = _SKIPPED_REASON
        return record


def check_scene(concept: str | None, scene_prompt: str) -> SceneGuard:
    """Screen `scene_prompt` against the foreground's cut `concept`.
    Pure: no store, no ledger, no call — the caller decides what a
    refusal means (draw refuses before spend)."""
    if concept is not None:
        concept = concept.strip() or None
    if concept is None:
        return SceneGuard("skipped", None, [])
    concept_tokens = _word_tokens(concept)
    if not concept_tokens:
        return SceneGuard("skipped", concept, [])

    head = concept_tokens[-1]
    checked_terms: list[str] = []
    if len(concept_tokens) > 1:
        checked_terms.append(" ".join(concept_tokens))
    checked_terms.append(head)

    scene_tokens = _word_tokens(scene_prompt)
    # The full phrase, as a token run (so "kids bike" does not fire
    # inside "kids biker"): the refusal names the whole concept.
    if len(concept_tokens) > 1 and _contains_token_run(scene_tokens, concept_tokens):
        return SceneGuard("refused", concept, checked_terms, offending_term=concept)
    # The head noun as a whole word, singular or with a trailing "s"
    # ("bike" / "bikes"): the refusal names the noun as the concept
    # wrote it.
    plural = f"{head}s"
    if any(token == head or token == plural for token in scene_tokens):
        head_as_written = [
            part for part in _split_words(concept) if part
        ][-1]
        return SceneGuard(
            "refused", concept, checked_terms, offending_term=head_as_written
        )
    return SceneGuard("pass", concept, checked_terms)


def _split_words(text: str) -> list[str]:
    """`text` split on non-alphanumerics, original casing kept."""
    parts: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        else:
            parts.append("".join(current))
            current = []
    parts.append("".join(current))
    return parts
