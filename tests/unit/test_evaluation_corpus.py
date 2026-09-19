"""The raw-input evaluation corpus and its independently declared ground truth.

PORT-004 exists because the previous suites scored *built-in predictions against
built-in labels*: ``evaluations.semantic`` copied the prediction list into the gold
list, and ``retrieval.golden`` built every fixture so the relevant candidate ranked
first. Both are fine regression tests and neither is evidence about the system.

These tests pin the half that was missing. They do not run the predictor — they
check that the corpus *is what it claims to be*: that its inputs are raw text, that
its labels are declared rather than computed, that the declared labels are
reproducible from the design doc's two published rules, and that every coverage
class the roadmap names is actually exercised.

The table below is the ground truth. Read it, do not re-derive it.
"""

from __future__ import annotations

import json
import re
from typing import cast

from backend.app.evaluations.corpus import (
    _ARCHETYPE_BY_KEY,
    _ARCHETYPES,
    _LABEL_LIMITATION,
    BUILTIN_CORPUS,
    CORPUS_NAME,
    CORPUS_VERSION,
    JOB_FAMILIES,
    CaseTag,
    CorpusCase,
    Split,
    _guarded,
    _text_backing_for,
    build_builtin_corpus,
)
from backend.app.infrastructure.model_gateway import _SKILL_KEYWORDS
from backend.app.reports.evidence_binding import _CN_YEARS_RE, _YEARS_RE
from backend.app.reports.models import SupportLevel
from backend.app.retrieval.education import EDUCATION_SURFACE_FORMS, education_canonical
from backend.app.retrieval.models import HardRuleId, HardRuleOutcome

_OVERALL = "hard_rule_overall"

#: archetype -> (years, education, skills, overall), each "OUTCOME/SUPPORT".
#:
#: This is the whole ground truth in twelve lines. Every value is a labeller's
#: reading of the case's raw text plus the two published rules (§9.4 text-backing,
#: §4.5 high-impact guard) — never a recorded prediction.
_EXPECTED_TABLE: dict[str, tuple[str, str, str, str]] = {
    "strong-match": ("PASS/SUPPORTED",) * 4,
    "case-variant-skills": ("PASS/SUPPORTED",) * 4,
    "extra-skills": ("PASS/SUPPORTED",) * 4,
    "out-of-vocabulary-skills": ("PASS/SUPPORTED",) * 4,
    "cross-segment-evidence": ("PASS/SUPPORTED",) * 4,
    "irrelevant-evidence": ("PASS/SUPPORTED",) * 4,
    "overlapping-dates": ("PASS/SUPPORTED",) * 4,
    # The verdict stands on confirmed fields; the resume never states the number.
    "unstated-but-confirmed": (
        "PASS/PARTIAL",
        "PASS/SUPPORTED",
        "PASS/SUPPORTED",
        "PASS/PARTIAL",
    ),
    "missing-education": (
        "PASS/SUPPORTED",
        "UNKNOWN/INSUFFICIENT",
        "PASS/SUPPORTED",
        "UNKNOWN/INSUFFICIENT",
    ),
    "missing-years": (
        "UNKNOWN/INSUFFICIENT",
        "PASS/SUPPORTED",
        "PASS/SUPPORTED",
        "UNKNOWN/INSUFFICIENT",
    ),
    # FAIL + unbacked => the §4.5 guard downgrades it, so INSUFFICIENT, not PARTIAL.
    "contradictory-years": (
        "FAIL/INSUFFICIENT",
        "PASS/SUPPORTED",
        "PASS/SUPPORTED",
        "FAIL/INSUFFICIENT",
    ),
    # FAIL + every observed value stated verbatim => the guard does not fire.
    "below-requirement": ("FAIL/SUPPORTED",) * 4,
    "unrelated-background": (
        "FAIL/SUPPORTED",
        "FAIL/SUPPORTED",
        "FAIL/INSUFFICIENT",
        "FAIL/INSUFFICIENT",
    ),
}

#: Which field a MISSING_FIELD case is actually missing, and from the *text*.
_MISSING_VALUE_BY_ARCHETYPE: dict[str, str] = {
    "missing-education": "education",
    "missing-years": "years",
}

_ALL_EDUCATION_FORMS: tuple[str, ...] = tuple(
    form for forms in EDUCATION_SURFACE_FORMS.values() for form in forms
)

#: Any spelled-out duration: "5 年", "5.5 年", "五年".
_ANY_DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*年")

_HOLDOUT_ARCHETYPES: frozenset[str] = frozenset(
    {
        "case-variant-skills",
        "out-of-vocabulary-skills",
        "cross-segment-evidence",
        "unstated-but-confirmed",
        "missing-years",
        "contradictory-years",
        "overlapping-dates",
        "below-requirement",
        "unrelated-background",
    }
)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _case(case_id: str) -> CorpusCase:
    by_id = {case.case_id: case for case in BUILTIN_CORPUS.cases}
    return by_id[case_id]


def _cases_of(archetype: str) -> tuple[CorpusCase, ...]:
    return tuple(case for case in BUILTIN_CORPUS.cases if case.archetype == archetype)


def _rule_claim(rule_id: HardRuleId) -> str:
    return f"hard_rule:{rule_id.value}"


def _label(case: CorpusCase, claim_type: str) -> str:
    item = case.gold_by_claim_type[claim_type]
    return f"{item.expected_outcome.value}/{item.expected_support.value}"


def _duration_values(text: str) -> set[float]:
    """Every numeric duration the locator would find, using the locator's own regex."""
    return {float(match.group(1)) for match in _YEARS_RE.finditer(text)}


def _sections_containing(case: CorpusCase, needle: str) -> list[str]:
    folded = needle.casefold()
    return [section for section, text in case.sections if folded in text.casefold()]


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_the_corpus_is_every_archetype_against_every_job_family() -> None:
    assert len(_ARCHETYPES) == 13
    assert len(JOB_FAMILIES) == 4
    assert len(BUILTIN_CORPUS.cases) == 52
    assert {case.archetype for case in BUILTIN_CORPUS.cases} == {
        archetype.key for archetype in _ARCHETYPES
    }


def test_case_ids_are_unique() -> None:
    ids = [case.case_id for case in BUILTIN_CORPUS.cases]
    assert len(set(ids)) == len(ids)


def test_the_split_is_dev_and_holdout_and_both_are_non_empty() -> None:
    dev = BUILTIN_CORPUS.split(Split.DEV)
    holdout = BUILTIN_CORPUS.split(Split.HOLDOUT)
    assert dev and holdout
    assert len(dev) + len(holdout) == len(BUILTIN_CORPUS.cases)
    assert {case.split for case in dev} == {Split.DEV}
    assert {case.split for case in holdout} == {Split.HOLDOUT}


def test_holdout_is_exactly_the_declared_holdout_archetypes() -> None:
    """What a threshold is *reported* against is what nobody iterated on."""
    assert {case.archetype for case in BUILTIN_CORPUS.split(Split.HOLDOUT)} == (_HOLDOUT_ARCHETYPES)


def test_the_reported_half_covers_every_declared_class() -> None:
    """The holdout half is what gets published, so it has to be able to measure all of it.

    A support level that only occurs in the dev half is a level the published number
    cannot be wrong about, and a coverage class that only occurs there is one the
    report never exercises. Both fail silently: the metrics still print.
    """
    holdout = BUILTIN_CORPUS.split(Split.HOLDOUT)
    assert {item.expected_outcome for case in holdout for item in case.conclusions} == set(
        HardRuleOutcome
    )
    assert {item.expected_support for case in holdout for item in case.conclusions} == set(
        SupportLevel
    )
    assert {tag for case in holdout for tag in case.tags} == set(CaseTag)


def test_every_case_tag_is_exercised() -> None:
    covered = {tag for case in BUILTIN_CORPUS.cases for tag in case.tags}
    assert covered == set(CaseTag)


def test_every_case_declares_all_four_conclusions_and_a_note() -> None:
    expected = {_rule_claim(rule) for rule in HardRuleId} | {_OVERALL}
    for case in BUILTIN_CORPUS.cases:
        assert set(case.gold_by_claim_type) == expected
        assert case.note.strip()
        for item in case.conclusions:
            assert item.note.strip()


def test_by_job_family_partitions_the_corpus() -> None:
    groups = [BUILTIN_CORPUS.by_job_family(family.key) for family in JOB_FAMILIES]
    assert [len(group) for group in groups] == [13, 13, 13, 13]
    ids = [case.case_id for group in groups for case in group]
    assert len(set(ids)) == len(BUILTIN_CORPUS.cases)


def test_the_declared_labels_span_every_outcome_and_every_support_level() -> None:
    """A corpus that never reaches PARTIAL cannot measure the middle level."""
    outcomes = {item.expected_outcome for case in BUILTIN_CORPUS.cases for item in case.conclusions}
    supports = {item.expected_support for case in BUILTIN_CORPUS.cases for item in case.conclusions}
    assert outcomes == set(HardRuleOutcome)
    assert supports == set(SupportLevel)


def test_every_job_family_has_relevant_and_irrelevant_cases() -> None:
    """A relevance set that is everything makes Recall@K 1.0 by construction."""
    for family in JOB_FAMILIES:
        cases = BUILTIN_CORPUS.by_job_family(family.key)
        assert any(case.relevant for case in cases)
        assert any(not case.relevant for case in cases)


def test_candidate_names_are_unique_within_a_job_family() -> None:
    for family in JOB_FAMILIES:
        names = [case.extraction.full_name for case in BUILTIN_CORPUS.by_job_family(family.key)]
        assert len(set(names)) == len(names)
        assert None not in names


# --------------------------------------------------------------------------- #
# The invariant that lets the gold be declared once per archetype
# --------------------------------------------------------------------------- #


def test_corpus_job_families_share_the_gate_thresholds() -> None:
    """Relax one family's bar and every declared verdict for it becomes wrong.

    That is the point: the shared thresholds are what justify writing the table
    once per archetype instead of once per (archetype, family) pair, so the
    assertion is what forces the gold to be re-derived rather than reinterpreted.
    """
    assert {family.min_years for family in JOB_FAMILIES} == {3.0}
    assert {family.required_education for family in JOB_FAMILIES} == {"本科"}


def test_no_case_satisfies_a_foreign_family_requirements() -> None:
    """Cross-family relevance is assumed false; this is what makes it safe.

    A query's candidate pool is every case while only same-family cases may be
    relevant. That is sound only if no case happens to hold another posting's
    required skills, so assert it rather than assume it.
    """
    for case in BUILTIN_CORPUS.cases:
        for family in JOB_FAMILIES:
            if family.key == case.job_family:
                continue
            missing = set(family.required_skills) - set(case.confirmed.normalized_skills)
            assert missing, f"{case.case_id} satisfies {family.key}"


# --------------------------------------------------------------------------- #
# The ground truth itself
# --------------------------------------------------------------------------- #


def test_declared_outcomes_match_the_pinned_table() -> None:
    for archetype, expected in _EXPECTED_TABLE.items():
        for case in _cases_of(archetype):
            actual = (
                _label(case, _rule_claim(HardRuleId.YEARS_EXPERIENCE)),
                _label(case, _rule_claim(HardRuleId.REQUIRED_EDUCATION)),
                _label(case, _rule_claim(HardRuleId.REQUIRED_SKILLS)),
                _label(case, _OVERALL),
            )
            assert actual == expected, case.case_id


def test_the_pinned_table_covers_every_archetype_in_the_corpus() -> None:
    """Adding an archetype without labelling it must fail, not go unmeasured."""
    present = {case.archetype for case in BUILTIN_CORPUS.cases}
    assert present == set(_EXPECTED_TABLE)


def test_declared_support_is_text_backing_after_the_high_impact_guard() -> None:
    """The label is composed from two named rules, not asserted independently.

    A labeller who reads §9.4 and §4.5 can reproduce every value in the table
    without running the pipeline — which is what "independently labelled" has to
    mean when the label describes a specified pipeline.
    """
    for case in BUILTIN_CORPUS.cases:
        archetype = _ARCHETYPE_BY_KEY[case.archetype]
        gold = case.gold_by_claim_type
        for rule_id in HardRuleId:
            backing = _text_backing_for(archetype, rule_id)
            expected = _guarded(backing, archetype.outcomes[rule_id])
            assert gold[_rule_claim(rule_id)].expected_support is expected, case.case_id


def test_the_high_impact_guard_changes_the_label_for_two_archetypes() -> None:
    """If the guard were a no-op, the composition above would prove nothing.

    Two archetypes reach it, for two different reasons: ``contradictory-years``
    states the wrong number, and ``unrelated-background`` has no number to state
    because its skills rule observed the empty set.
    """
    downgraded = {
        archetype.key
        for archetype in _ARCHETYPES
        if any(
            _guarded(_text_backing_for(archetype, rule), archetype.outcomes[rule])
            is not _text_backing_for(archetype, rule)
            for rule in HardRuleId
        )
    }
    assert downgraded == {"contradictory-years", "unrelated-background"}


def test_outcome_and_support_are_independent_facts() -> None:
    """Two archetypes both FAIL the years rule and are labelled differently.

    If support were a function of the verdict these two would be indistinguishable,
    and the report's "the verdict stands but nothing backs it" case could not exist.
    """
    claim = _rule_claim(HardRuleId.YEARS_EXPERIENCE)
    below = _case("below-requirement@python-backend").gold_by_claim_type[claim]
    contradictory = _case("contradictory-years@python-backend").gold_by_claim_type[claim]

    assert below.expected_outcome is HardRuleOutcome.FAIL
    assert contradictory.expected_outcome is HardRuleOutcome.FAIL
    assert below.expected_support is SupportLevel.SUPPORTED
    assert contradictory.expected_support is SupportLevel.INSUFFICIENT


def test_the_label_limitation_is_recorded_and_published() -> None:
    assert _LABEL_LIMITATION.strip()
    assert BUILTIN_CORPUS.content()["label_limitation"] == _LABEL_LIMITATION


def test_the_label_limitation_is_exercised_by_a_case() -> None:
    """A FAIL whose observed value is stated verbatim is SUPPORTED, not unbacked.

    That is the limitation: the excerpt proves the *value*, never the negative.
    """
    claim = _rule_claim(HardRuleId.REQUIRED_SKILLS)
    gold = _case("below-requirement@python-backend").gold_by_claim_type[claim]
    assert gold.expected_outcome is HardRuleOutcome.FAIL
    assert gold.expected_support is SupportLevel.SUPPORTED


def test_unknown_rules_have_no_value_to_locate() -> None:
    """UNKNOWN means the field is absent, so nothing in the text could back it."""
    for case in BUILTIN_CORPUS.cases:
        gold = case.gold_by_claim_type
        for rule_id in HardRuleId:
            if gold[_rule_claim(rule_id)].expected_outcome is not HardRuleOutcome.UNKNOWN:
                continue
            if rule_id is HardRuleId.YEARS_EXPERIENCE:
                assert case.confirmed.years_experience is None, case.case_id
            elif rule_id is HardRuleId.REQUIRED_EDUCATION:
                assert case.confirmed.education_level is None, case.case_id
            else:
                assert not case.confirmed.normalized_skills, case.case_id


# --------------------------------------------------------------------------- #
# The inputs are raw text, and the gold describes that text
# --------------------------------------------------------------------------- #


def test_extraction_gold_is_reachable_from_the_resume_text() -> None:
    """A gold value that is not in the text is a labelling error, not a prediction."""
    for case in BUILTIN_CORPUS.cases:
        text = case.resume_text.casefold()
        name = case.extraction.full_name
        assert name is not None, case.case_id
        assert name.casefold() in text, case.case_id
        for skill in case.extraction.skills:
            assert skill.casefold() in text, (case.case_id, skill)


def test_at_least_one_declared_skill_is_outside_the_heuristic_vocabulary() -> None:
    """Otherwise the mock-mode extraction score is 1.0 by construction.

    If every skill in the corpus came from the fake extractor's hard-coded list, a
    perfect extraction score would say nothing: a reader could not tell "the
    extractor is right" from "the gold was written from the same list".
    """
    declared = {skill for case in BUILTIN_CORPUS.cases for skill in case.extraction.skills}
    assert declared - set(_SKILL_KEYWORDS)


def test_extraction_education_is_derived_from_the_resume_spelling() -> None:
    """The extractor's only correct answer for 本科 is the closed enum BACHELOR."""
    for case in BUILTIN_CORPUS.cases:
        surface = _ARCHETYPE_BY_KEY[case.archetype].education_surface
        canonical = education_canonical(surface)
        assert case.extraction.education_level == (
            canonical.upper() if canonical is not None else None
        ), case.case_id


def test_confirmed_education_is_the_same_level_spelled_differently() -> None:
    """The profile stores the enum, the resume writes the Chinese surface form."""
    for case in BUILTIN_CORPUS.cases:
        surface = _ARCHETYPE_BY_KEY[case.archetype].education_surface
        assert education_canonical(case.confirmed.education_level) == education_canonical(
            surface
        ), case.case_id


def test_every_resume_opens_with_the_name_as_its_own_line() -> None:
    """A name folded into a summary sentence is not a name field."""
    for case in BUILTIN_CORPUS.cases:
        first_section, first_text = case.sections[0]
        assert first_section == "name", case.case_id
        assert first_text == case.extraction.full_name, case.case_id


def test_missing_field_archetypes_omit_the_value_from_the_text() -> None:
    """Missing from the text, not merely missing from the approved profile."""
    for archetype, field in _MISSING_VALUE_BY_ARCHETYPE.items():
        case = _case(f"{archetype}@python-backend")
        if field == "education":
            assert case.confirmed.education_level is None
            assert not any(form in case.resume_text for form in _ALL_EDUCATION_FORMS)
        else:
            assert case.confirmed.years_experience is None
            assert _ANY_DURATION_RE.search(case.resume_text) is None


def test_synonym_case_spells_the_skills_differently_from_the_profile() -> None:
    case = _case("case-variant-skills@python-backend")
    assert "PYTHON" in case.resume_text
    assert "python" not in case.resume_text
    assert case.confirmed.normalized_skills == ("python", "postgresql")


def test_contradictory_years_text_states_a_value_the_profile_does_not_hold() -> None:
    """The locator cannot find 2.0 because the resume only ever says 8."""
    case = _case("contradictory-years@python-backend")
    assert case.confirmed.years_experience == 2.0
    assert _duration_values(case.resume_text) == {8.0}


def test_overlapping_dates_distinguish_the_sum_from_the_union() -> None:
    """Two ranges, 5 years added up and 4 years of actual coverage."""
    case = _case("overlapping-dates@python-backend")
    ranges = ((2022, 2024), (2023, 2026))
    naive_sum = sum(end - start for start, end in ranges)
    union = max(end for _, end in ranges) - min(start for start, _ in ranges)
    assert (naive_sum, union) == (5, 4)
    assert case.confirmed.years_experience == union
    assert _duration_values(case.resume_text) == {float(union)}


def test_unstated_but_confirmed_never_writes_the_number() -> None:
    """The reviewer's 5.0 comes from the dates; the text never says "5 年"."""
    case = _case("unstated-but-confirmed@python-backend")
    assert case.confirmed.years_experience == 5.0
    assert _duration_values(case.resume_text) == set()
    assert _CN_YEARS_RE.search(case.resume_text) is None


def test_unrelated_background_holds_none_of_the_required_skills() -> None:
    case = _case("unrelated-background@python-backend")
    assert case.confirmed.normalized_skills == ()
    assert case.extraction.skills == frozenset()
    for skill in ("python", "postgresql"):
        assert skill not in case.resume_text


def test_cross_segment_evidence_puts_each_value_in_its_own_section() -> None:
    """Each conclusion must hit its own segment, not a shared first paragraph."""
    case = _case("cross-segment-evidence@python-backend")
    owners = {needle: _sections_containing(case, needle) for needle in ("本科", "python", "4 年")}
    for needle, sections in owners.items():
        assert len(sections) == 1, (needle, sections)
    assert len({sections[0] for sections in owners.values()}) == 3


def test_irrelevant_evidence_puts_an_uncitable_section_first() -> None:
    """A binder that fell back to ``chunks[0]`` would cite the hobby line."""
    case = _case("irrelevant-evidence@python-backend")
    order = [section for section, _ in case.sections]
    hobby_index = order.index("hobby")
    for section in ("education", "skill", "experience"):
        assert hobby_index < order.index(section)

    hobby_text = dict(case.sections)["hobby"]
    for needle in ("本科", "python", "postgresql", "5 年"):
        assert needle.casefold() not in hobby_text.casefold()


# --------------------------------------------------------------------------- #
# Identity, projection and the hashed payload
# --------------------------------------------------------------------------- #


def test_profile_ids_are_unique_across_cases() -> None:
    """A truncated case id collided across job families; uuid5 does not."""
    ids = [case.profile_id for case in BUILTIN_CORPUS.cases]
    assert len(set(ids)) == len(ids)


def test_profile_ids_are_stable_across_builds() -> None:
    rebuilt = {case.case_id: case.profile_id for case in build_builtin_corpus().cases}
    for case in BUILTIN_CORPUS.cases:
        assert rebuilt[case.case_id] == case.profile_id


def test_ready_profile_mirrors_the_confirmed_fields() -> None:
    case = _case("below-requirement@data-platform")
    profile = case.ready_profile()
    assert profile.profile_id == case.profile_id
    assert profile.normalized_skills == list(case.confirmed.normalized_skills)
    assert profile.years_experience == case.confirmed.years_experience
    assert profile.education_level == case.confirmed.education_level
    assert profile.profile_json["education_level"] == case.extraction.education_level


def test_job_query_carries_the_family_requirements() -> None:
    case = _case("strong-match@python-backend")
    family = next(family for family in JOB_FAMILIES if family.key == "python-backend")
    assert case.job.job_version_id == family.job_version_id
    assert case.job.required_skills == list(family.required_skills)
    assert case.job.preferred_skills == list(family.preferred_skills)
    assert case.job.min_years == 3.0
    assert case.job.required_education == "本科"


def test_building_the_corpus_twice_produces_identical_content() -> None:
    assert build_builtin_corpus().content() == BUILTIN_CORPUS.content()


def test_content_carries_the_inputs_and_the_gold_not_a_manifest() -> None:
    """A hash over a description would mean "same description", not "same evaluation"."""
    content = BUILTIN_CORPUS.content()
    assert content["name"] == CORPUS_NAME
    assert content["version"] == CORPUS_VERSION
    assert content["label_limitation"] == _LABEL_LIMITATION

    cases = cast("list[dict[str, object]]", content["cases"])
    assert len(cases) == len(BUILTIN_CORPUS.cases)
    first = cases[0]
    assert first["resume_text"] == BUILTIN_CORPUS.cases[0].resume_text
    assert first["confirmed"]
    assert first["extraction"]
    assert first["conclusions"]


def test_content_is_json_serializable() -> None:
    """The payload feeds a content hash, so it must round-trip through JSON."""
    payload = json.dumps(BUILTIN_CORPUS.content(), ensure_ascii=False, sort_keys=True)
    assert json.loads(payload)["cases"]
