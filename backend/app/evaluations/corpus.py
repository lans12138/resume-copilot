"""The raw-input evaluation corpus with independently labelled ground truth.

PORT-004's central defect is that the existing suites score *built-in predictions
against built-in labels*. Two of them are worth stating plainly, because they are
the reason this module exists:

* ``evaluations.semantic._BUILTIN_GOLD = list(_BUILTIN_PREDICTED)`` — the gold
  labels are a copy of the predictions, so the semantic suite scores 1.0 by
  construction. It is a test of the metric arithmetic, not of the system.
* ``retrieval.golden._make_case`` builds the recall bundle so that relevant
  candidates rank first in every channel ("threshold-guaranteed"), so
  ``evaluate_builtin`` measures RRF arithmetic over a fixture designed to pass.

Neither is wrong as a regression test. Both are wrong as *evidence about the
system*, and the roadmap's problem table says exactly that.

This module supplies the missing half: cases whose inputs are **raw resume text
and a job posting**, and whose answers are **declared here, by hand, from the
case's design intent** — never computed by running the predictor. A case states
what the resume says; the gold states what a careful labeller would conclude from
it. If the predictor disagrees, that is the signal.

Three deliberate separations make the labels checkable:

**Extraction gold vs confirmed profile.** The pipeline is "upload → extract →
human proofread → retrieve". Extraction quality is measured against
``extraction`` (what a correct extractor produces from the raw text); everything
downstream runs on ``confirmed`` (what the reviewer approved). Collapsing the two
would make a proofreading correction look like an extraction error.

**Outcome gold vs support gold.** ``outcome`` is the hard-rule verdict
(PASS/FAIL/UNKNOWN) a labeller expects from the confirmed fields; ``support`` is
whether the resume text backs the claim that reports it. They are separate
questions and the corpus declares them separately, which is what lets the
evaluation tell "the rule is wrong" apart from "the citation is wrong".

**Support is about the observed value, not the verdict.** A claim's citation
proves the *observed value* is real — that the candidate's own text says "5 年" —
not that the verdict derived from it is right. So a FAIL claim whose observed
value is stated verbatim is labelled SUPPORTED here. This is a declared
limitation of the label set, not an oversight: "the candidate lacks skill X" is a
set difference over the confirmed profile and no excerpt can establish it. See
``_LABEL_LIMITATION`` below.

**The declared support level is post-guard.** Two published rules compose into it,
and both are named in the code so a reviewer can follow them: §9.4 decides whether
the text states the observed value (``_text_backing_for``), then §4.5 downgrades a
HIGH-impact claim that is not SUPPORTED to INSUFFICIENT (``_guarded``). Declaring
only the §9.4 half would make every unbacked FAIL — of which the corpus has one —
read as a prediction error and bury the errors that are real. Declaring the
composed level keeps the label predictable from the design doc alone, which is what
"independently labelled" has to mean when the label describes a specified pipeline.

Coverage. Every case carries :class:`CaseTag` values, and the six classes the
roadmap names — synonym spelling, missing fields, cross-segment evidence,
contradictory text, overlapping dates, irrelevant evidence — are each exercised
by at least one archetype. ``tests/unit/test_evaluation_corpus.py`` asserts the
coverage and pins the declared outcomes, so a reviewer can check the ground truth
by reading one table rather than by re-running the predictor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid5

from backend.app.reports.models import SupportLevel
from backend.app.retrieval.education import education_canonical
from backend.app.retrieval.models import (
    HardRuleId,
    HardRuleOutcome,
    JobQuery,
    ReadyProfile,
)

#: The label set's declared blind spot. Recorded as a module constant so it can be
#: asserted in a test and quoted in the evaluation report, rather than living only
#: in a comment someone will eventually delete.
_LABEL_LIMITATION = (
    "support labels judge whether the observed value is stated in the candidate's "
    "own text; they do not establish a negative (that a required skill is absent) "
    "from a positive excerpt"
)


class Split(StrEnum):
    """Which half of the corpus a case belongs to.

    Development cases are what a threshold may be tuned against. Holdout cases are
    what a threshold is *reported* against, and are deliberately not the cases
    anyone iterates on — that is the whole reason for the split.
    """

    DEV = "DEV"
    HOLDOUT = "HOLDOUT"


class CaseTag(StrEnum):
    """A coverage class a case exercises. Used to prove coverage, not to filter."""

    SYNONYM_SKILL = "SYNONYM_SKILL"
    MISSING_FIELD = "MISSING_FIELD"
    CROSS_SEGMENT_EVIDENCE = "CROSS_SEGMENT_EVIDENCE"
    CONTRADICTORY_TEXT = "CONTRADICTORY_TEXT"
    OVERLAPPING_DATES = "OVERLAPPING_DATES"
    IRRELEVANT_EVIDENCE = "IRRELEVANT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class JobFamily:
    """One of the job postings the corpus candidates are matched against."""

    key: str
    title: str
    required_skills: tuple[str, ...]
    preferred_skills: tuple[str, ...]
    min_years: float
    required_education: str
    job_version_id: UUID

    def to_query(self) -> JobQuery:
        return JobQuery(
            job_version_id=self.job_version_id,
            required_skills=list(self.required_skills),
            preferred_skills=list(self.preferred_skills),
            min_years=self.min_years,
            required_education=self.required_education,
            description_text=f"{self.title}；必备技能：" + "、".join(self.required_skills),
            requirements_json={},
        )


@dataclass(frozen=True, slots=True)
class ExtractionGold:
    """What a correct extraction of the raw resume text produces."""

    full_name: str | None
    skills: frozenset[str]
    education_level: str | None


@dataclass(frozen=True, slots=True)
class ConfirmedProfile:
    """The reviewer-approved fields the rest of the pipeline runs on."""

    normalized_skills: tuple[str, ...]
    years_experience: float | None
    education_level: str | None


@dataclass(frozen=True, slots=True)
class ConclusionGold:
    """One hand-labelled conclusion for one case."""

    claim_type: str
    expected_outcome: HardRuleOutcome
    expected_support: SupportLevel
    note: str


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One raw input plus the answers a labeller declares for it."""

    case_id: str
    split: Split
    tags: frozenset[CaseTag]
    archetype: str
    job_family: str
    sections: tuple[tuple[str, str], ...]
    job: JobQuery
    extraction: ExtractionGold
    confirmed: ConfirmedProfile
    relevant: bool
    conclusions: tuple[ConclusionGold, ...]
    note: str

    @property
    def resume_text(self) -> str:
        """The raw document, as the extractor receives it."""
        return "\n".join(text for _section, text in self.sections)

    @property
    def profile_id(self) -> UUID:
        """A stable id derived from the case id, so a run needs no side table."""
        return _profile_id(self.case_id)

    @property
    def gold_by_claim_type(self) -> dict[str, ConclusionGold]:
        return {item.claim_type: item for item in self.conclusions}

    def ready_profile(self) -> ReadyProfile:
        """The retrieval projection the pipeline runs on.

        Built from ``confirmed``, never from ``extraction``: recall, hard rules and
        reports all consume what the reviewer approved, and feeding them the
        extractor's raw output would silently measure a pipeline that does not
        exist. ``profile_json`` is the extracted draft, because that is what
        production stores — keyword recall tokenizes it.
        """
        return ReadyProfile(
            profile_id=self.profile_id,
            normalized_skills=list(self.confirmed.normalized_skills),
            years_experience=self.confirmed.years_experience,
            education_level=self.confirmed.education_level,
            profile_json={
                "full_name": self.extraction.full_name,
                "skills": [{"name": skill} for skill in sorted(self.extraction.skills)],
                "education_level": self.extraction.education_level,
            },
        )


@dataclass(frozen=True, slots=True)
class EvaluationCorpus:
    """A versioned set of raw-input cases."""

    name: str
    version: str
    note: str
    cases: tuple[CorpusCase, ...]

    def split(self, which: Split) -> tuple[CorpusCase, ...]:
        return tuple(case for case in self.cases if case.split is which)

    def by_job_family(self, key: str) -> tuple[CorpusCase, ...]:
        """The cases that belong to one posting.

        One job family is one recall query: its candidates are the corpus the
        channels rank, and each case's ``relevant`` flag is that query's relevance
        judgement. Grouping this way is what lets Recall@K / MRR / nDCG be computed
        per posting instead of over a pool that mixes four unrelated requirements.
        """
        return tuple(case for case in self.cases if case.job_family == key)

    def content(self) -> dict[str, object]:
        """The payload the dataset's ``content_hash`` is computed over.

        Carries the case *inputs and gold*, not just a manifest. FIN-007's
        ``manifest_json`` is deliberately a bounded description, but the hash has
        to cover the thing being evaluated or "same version" would mean "same
        description", which is not the same claim.
        """
        return {
            "name": self.name,
            "version": self.version,
            "note": self.note,
            "label_limitation": _LABEL_LIMITATION,
            "cases": [
                {
                    "case_id": case.case_id,
                    "split": case.split.value,
                    "archetype": case.archetype,
                    "job_family": case.job_family,
                    "tags": sorted(tag.value for tag in case.tags),
                    "resume_text": case.resume_text,
                    "confirmed": {
                        "normalized_skills": list(case.confirmed.normalized_skills),
                        "years_experience": case.confirmed.years_experience,
                        "education_level": case.confirmed.education_level,
                    },
                    "extraction": {
                        "full_name": case.extraction.full_name,
                        "skills": sorted(case.extraction.skills),
                        "education_level": case.extraction.education_level,
                    },
                    "relevant": case.relevant,
                    "conclusions": [
                        {
                            "claim_type": item.claim_type,
                            "outcome": item.expected_outcome.value,
                            "support": item.expected_support.value,
                        }
                        for item in case.conclusions
                    ],
                }
                for case in self.cases
            ],
        }


# --------------------------------------------------------------------------- #
# Job families
# --------------------------------------------------------------------------- #
#
# All four share ``min_years = 3.0`` and ``required_education = 本科`` on purpose.
# With the thresholds held constant, a candidate archetype's hard-rule outcome is
# the same against every family, so the declared gold below can be written once
# per archetype instead of once per (archetype, family) pair — fifty-two declarations
# nobody would read, versus ten a reviewer can check. The invariant is not left to
# trust: ``test_corpus_job_families_share_the_gate_thresholds`` asserts it, so
# relaxing one family's bar forces the gold to be re-derived.

JOB_FAMILIES: tuple[JobFamily, ...] = (
    JobFamily(
        key="python-backend",
        title="Python 后端工程师",
        required_skills=("python", "postgresql"),
        preferred_skills=("docker", "fastapi"),
        min_years=3.0,
        required_education="本科",
        job_version_id=UUID("0f000000-0000-4000-8000-000000000001"),
    ),
    JobFamily(
        key="java-payments",
        title="Java 支付系统工程师",
        required_skills=("java", "spring"),
        preferred_skills=("kafka", "redis"),
        min_years=3.0,
        required_education="本科",
        job_version_id=UUID("0f000000-0000-4000-8000-000000000002"),
    ),
    JobFamily(
        key="data-platform",
        title="数据平台工程师",
        required_skills=("python", "sql"),
        preferred_skills=("pandas", "kubernetes"),
        min_years=3.0,
        required_education="本科",
        job_version_id=UUID("0f000000-0000-4000-8000-000000000003"),
    ),
    JobFamily(
        key="frontend-web",
        title="前端工程师",
        required_skills=("typescript", "react"),
        preferred_skills=("node", "vue"),
        min_years=3.0,
        required_education="本科",
        job_version_id=UUID("0f000000-0000-4000-8000-000000000004"),
    ),
)

_JOB_BY_KEY = {family.key: family for family in JOB_FAMILIES}

#: Profile ids are derived from the case id so a run is reproducible and a report
#: can be traced back to a case without a side table.
_PROFILE_NAMESPACE = UUID("0c000000-0000-4000-8000-000000000000")


def _profile_id(case_id: str) -> UUID:
    """A stable id derived from the case id.

    ``uuid5`` over a fixed namespace: deterministic across processes and interpreter
    versions (unlike ``hash()``, which is salted per process) and injective for
    distinct case ids. A hand-rolled truncation of the case id was the first attempt
    and it collided across job families — every archetype shares its first twelve
    characters with its own siblings in the other three families.
    """
    return uuid5(_PROFILE_NAMESPACE, case_id)


@dataclass(frozen=True, slots=True)
class _Archetype:
    """A resume archetype: raw text, confirmed fields, and the declared answers.

    ``locatable`` names the rule ids whose *observed value* the resume states
    verbatim. It is what the support label is derived from, and it is a property of
    the archetype rather than of any run: a case either writes "5 年" in the text or
    it does not.

    ``education_level`` is the *confirmed* value and therefore the closed English
    token ``CandidateProfile`` stores (``BACHELOR``), while ``education_surface`` is
    how the resume spells it (``本科``). Keeping both is not pedantry: the job's
    requirement carries the recruiter's spelling and the profile carries the enum,
    so every education verdict in this corpus crosses that boundary. A corpus that
    used the same spelling on both sides would never exercise it.
    """

    key: str
    split: Split
    tags: tuple[CaseTag, ...]
    note: str
    sections: tuple[tuple[str, str], ...]
    skills: tuple[str, ...]
    years_experience: float | None
    education_level: str | None
    education_surface: str | None
    outcomes: dict[HardRuleId, HardRuleOutcome]
    locatable: frozenset[HardRuleId]
    relevant: bool


def _required_skills_text(family: JobFamily) -> str:
    return "、".join(family.required_skills)


# --------------------------------------------------------------------------- #
# Archetypes
# --------------------------------------------------------------------------- #
#
# ``outcomes`` is the hand-declared hard-rule verdict. It is written here from the
# confirmed fields and the shared thresholds — a labeller's reading, not a
# computation — and pinned by a test so it cannot drift silently.

_ARCHETYPES: tuple[_Archetype, ...] = (
    _Archetype(
        key="strong-match",
        split=Split.DEV,
        tags=(),
        note="全部字段写明且满足要求；基线正例。",
        sections=(
            ("summary", "{name}｜{years} 年工作经验｜学历：{education}"),
            ("skill", "技能：{required}"),
            ("experience", "工作经历：{years} 年，主要负责后端服务开发与维护。"),
        ),
        skills=("__required__",),
        years_experience=6.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="case-variant-skills",
        split=Split.DEV,
        tags=(CaseTag.SYNONYM_SKILL,),
        note="技能只写大小写/全半角变体，验证归一化后仍能匹配并定位原文。",
        sections=(
            ("summary", "{name}｜{years} 年工作经验｜学历：{education}"),
            ("skill", "技能：{required_upper}"),
            ("experience", "工作经历：{years} 年，从事平台开发。"),
        ),
        skills=("__required__",),
        years_experience=5.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="extra-skills",
        split=Split.DEV,
        tags=(CaseTag.IRRELEVANT_EVIDENCE,),
        note=(
            "除必备技能外还写了若干与岗位无关的技能，验证多余观测值不会破坏引用定位。"
            "干扰项刻意选用抽取词表内的英文技术词：换成词表外的词，抽取召回率的缺口就来自"
            "抽取器的词表，而不是本用例要考察的定位逻辑。"
        ),
        sections=(
            ("summary", "{name}｜{years} 年工作经验｜学历：{education}"),
            ("skill", "技能：{required}、rust、docker、linux"),
            ("hobby", "个人爱好：马拉松、摄影、法语角。"),
            ("experience", "工作经历：{years} 年，负责系统设计与交付。"),
        ),
        skills=("__required__", "rust", "docker", "linux"),
        years_experience=7.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="missing-education",
        split=Split.DEV,
        tags=(CaseTag.MISSING_FIELD,),
        note="原文与确认字段都没有学历 → 学历规则 UNKNOWN，结论必须为「证据不足」。",
        sections=(
            ("summary", "{name}｜{years} 年工作经验"),
            ("skill", "技能：{required}"),
            ("experience", "工作经历：{years} 年，负责系统开发与维护。"),
        ),
        skills=("__required__",),
        years_experience=5.0,
        education_level=None,
        education_surface=None,
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.UNKNOWN,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset({HardRuleId.YEARS_EXPERIENCE, HardRuleId.REQUIRED_SKILLS}),
        relevant=True,
    ),
    _Archetype(
        key="cross-segment-evidence",
        split=Split.DEV,
        tags=(CaseTag.CROSS_SEGMENT_EVIDENCE,),
        note="年限、技能、学历分处三个段落，验证每个结论各自命中自己那一段。",
        sections=(
            ("education", "教育背景：{education}，计算机相关专业"),
            ("skill", "技能：{required}"),
            ("experience", "工作经历：{years} 年，负责后端与数据服务。"),
        ),
        skills=("__required__",),
        years_experience=4.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="irrelevant-evidence",
        split=Split.DEV,
        tags=(CaseTag.IRRELEVANT_EVIDENCE,),
        note="首段与所有规则无关，验证引用不会退化为「引用第一段」。",
        sections=(
            ("hobby", "个人爱好：马拉松、摄影"),
            ("education", "教育背景：{education}，计算机相关专业"),
            ("skill", "技能：{required}"),
            ("experience", "工作经历：{years} 年，负责后端服务。"),
        ),
        skills=("__required__",),
        years_experience=5.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="unstated-but-confirmed",
        split=Split.DEV,
        tags=(),
        note=(
            "复核者按经历日期确认年限为 5.0，但原文从未写出「5 年」→ 结论成立（PASS）"
            "而支持等级只有 PARTIAL。这是 §9.4 三档中的中间档，也是「结论对」与"
            "「引用对」必须分开度量的原因。"
        ),
        sections=(
            ("summary", "{name}｜后端工程师｜学历：{education}"),
            ("skill", "技能：{required}"),
            # Dates only. Writing "5 年" would make the value locatable and collapse
            # this case into strong-match; the whole point is that the reviewer's
            # number is right while the resume never says it.
            ("experience", "工作经历：2020.03 - 2025.03，任后端开发工程师。"),
        ),
        skills=("__required__",),
        years_experience=5.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset({HardRuleId.REQUIRED_EDUCATION, HardRuleId.REQUIRED_SKILLS}),
        relevant=True,
    ),
    _Archetype(
        key="out-of-vocabulary-skills",
        split=Split.DEV,
        tags=(),
        note=(
            "必备技能之外还写了抽取词表里没有的技能（kotlin、grpc）。抽取召回率因此"
            "达不到满分——这是刻意的：如果每一项技能都取自同一个词表，Mock 模式下的"
            "抽取指标会恒等于 1.0，读者就无法分辨「抽取确实正确」和「标准答案照着词表写」。"
        ),
        sections=(
            ("summary", "{name}｜{years} 年工作经验｜学历：{education}"),
            ("skill", "技能：{required}、kotlin、grpc"),
            ("experience", "工作经历：{years} 年，负责服务端与接口开发。"),
        ),
        skills=("__required__", "kotlin", "grpc"),
        years_experience=6.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="missing-years",
        split=Split.HOLDOUT,
        tags=(CaseTag.MISSING_FIELD,),
        note="全文没有任何年限信息，确认字段也为空 → 年限规则 UNKNOWN。",
        sections=(
            ("education", "教育背景：{education}，计算机相关专业"),
            ("skill", "技能：{required}"),
            ("experience", "工作经历：负责后端服务开发。"),
        ),
        skills=("__required__",),
        years_experience=None,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.UNKNOWN,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset({HardRuleId.REQUIRED_EDUCATION, HardRuleId.REQUIRED_SKILLS}),
        relevant=True,
    ),
    _Archetype(
        key="contradictory-years",
        split=Split.HOLDOUT,
        tags=(CaseTag.CONTRADICTORY_TEXT,),
        note=(
            "摘要写 8 年，经历日期只覆盖 2 年；复核者按日期确认为 2.0，该观测值未在原文中"
            "出现 → 结论「不满足」属高风险且无引用，按 §4.5 降为「证据不足」。"
        ),
        sections=(
            ("summary", "{name}｜8 年工作经验｜学历：{education}"),
            ("skill", "技能：{required}"),
            # No "2 年" here on purpose: the reviewer derived 2.0 from the date range,
            # so the resume never states the value the rule reports. Writing it out
            # would collapse this case into below-requirement.
            ("experience", "工作经历：2024.01 - 2026.01，任后端开发工程师。"),
        ),
        skills=("__required__",),
        years_experience=2.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.FAIL,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        # The observed value 2.0 is *not* stated: the text says 8. This is the case
        # that makes "locatable" a separate fact from "outcome".
        locatable=frozenset({HardRuleId.REQUIRED_EDUCATION, HardRuleId.REQUIRED_SKILLS}),
        relevant=False,
    ),
    _Archetype(
        key="overlapping-dates",
        split=Split.HOLDOUT,
        tags=(CaseTag.OVERLAPPING_DATES,),
        note=(
            "两段经历时间重叠：按区间长度直接相加为 5 年，复核者按并集确认为 4 年；"
            "摘要写明「约 4 年」，观测值可定位。"
        ),
        sections=(
            ("summary", "{name}｜约 4 年工作经验｜学历：{education}"),
            ("skill", "技能：{required}"),
            (
                "experience",
                "工作经历：2022.01 - 2024.01 A 公司；2023.01 - 2026.01 B 公司（时间重叠）。",
            ),
        ),
        skills=("__required__",),
        years_experience=4.0,
        education_level="BACHELOR",
        education_surface="本科",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.PASS,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.PASS,
        },
        locatable=frozenset(HardRuleId),
        relevant=True,
    ),
    _Archetype(
        key="below-requirement",
        split=Split.HOLDOUT,
        # Deliberately untagged: nothing is missing and nothing is contradictory. This
        # is the plain all-FAIL baseline, and it exists to pair with
        # contradictory-years — same verdict, different support level.
        tags=(),
        note=(
            "年限、学历、技能三项均不满足；三项观测值都写在原文中，"
            "因此高风险守卫不触发，支持等级仍是 SUPPORTED。"
        ),
        sections=(
            ("summary", "{name}｜1 年工作经验｜学历：大专"),
            ("skill", "技能：{first_required}"),
            ("experience", "工作经历：1 年，参与过内部工具开发。"),
        ),
        skills=("__first_required__",),
        years_experience=1.0,
        education_level="ASSOCIATE",
        education_surface="大专",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.FAIL,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.FAIL,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.FAIL,
        },
        locatable=frozenset(HardRuleId),
        relevant=False,
    ),
    _Archetype(
        key="unrelated-background",
        split=Split.HOLDOUT,
        tags=(CaseTag.IRRELEVANT_EVIDENCE,),
        note=(
            "履历与岗位要求完全无关：一项必备技能都没有，年限与学历也不达标。"
            "技能规则因此没有任何可引用的观测值（观测值是空集），按 §4.5 降为「证据不足」——"
            "这正是标签局限的实例：「候选人缺少技能 X」是集合差，正向片段无法证明。"
        ),
        sections=(
            ("summary", "{name}｜{years} 年工作经验｜学历：{education}"),
            ("experience", "工作经历：{years} 年，从事行政与后勤管理。"),
        ),
        skills=(),
        years_experience=1.0,
        education_level="HIGH_SCHOOL",
        education_surface="高中",
        outcomes={
            HardRuleId.YEARS_EXPERIENCE: HardRuleOutcome.FAIL,
            HardRuleId.REQUIRED_EDUCATION: HardRuleOutcome.FAIL,
            HardRuleId.REQUIRED_SKILLS: HardRuleOutcome.FAIL,
        },
        # The skills rule's observed value is the empty set, so there is nothing for
        # the locator to find and the label is not SUPPORTED.
        locatable=frozenset({HardRuleId.YEARS_EXPERIENCE, HardRuleId.REQUIRED_EDUCATION}),
        relevant=False,
    ),
)

_ARCHETYPE_BY_KEY = {archetype.key: archetype for archetype in _ARCHETYPES}

#: Names are assigned round-robin across the (archetype, family) grid, so the list
#: must be at least as long as the number of archetypes or two cases inside one
#: posting would share a name. Kept comfortably longer than needed; a test asserts
#: the uniqueness that the margin exists to protect.
_CANDIDATE_NAMES: tuple[str, ...] = (
    "张伟",
    "李娜",
    "王强",
    "刘洋",
    "陈静",
    "杨帆",
    "赵磊",
    "孙悦",
    "周涛",
    "吴敏",
    "郑凯",
    "何雨",
    "许峰",
    "邓丽",
    "冯超",
    "曹雪",
)


def _render_sections(
    archetype: _Archetype, family: JobFamily, *, name: str
) -> tuple[tuple[str, str], ...]:
    """Fill an archetype's templates for one job family.

    The name is prepended as its own section rather than folded into the summary
    line. A resume heading is how the document actually looks, and it is also what
    makes the name field reachable at all: a name buried mid-sentence inside
    ``张伟｜6 年工作经验｜学历：本科`` is not a name field, it is a paragraph, and no
    extractor should be scored as though it were one.
    """
    required = _required_skills_text(family)
    first_required = family.required_skills[0]
    substitutions = {
        "name": name,
        "years": _years_phrase(archetype.years_experience),
        "education": archetype.education_surface or "",
        "required": required,
        "required_upper": required.upper(),
        "first_required": first_required,
    }
    return (
        ("name", name),
        *((section, text.format(**substitutions)) for section, text in archetype.sections),
    )


def _years_phrase(years: float | None) -> str:
    """The literal a template interpolates for ``{years}``.

    ``None`` renders as 若干 rather than an empty string so a template that
    interpolates it can never produce "｜ 年工作经验". An archetype whose text must
    *disagree* with the confirmed value does not interpolate it at all — see
    ``contradictory-years``, whose summary hard-codes "8 年" while the reviewer
    confirmed 2.0.
    """
    if years is None:
        return "若干"
    return f"{years:g}"


def _education_token(surface: str | None) -> str | None:
    """The enum token a correct extractor returns for a spelled-out level.

    Derived from the resume's own spelling rather than declared beside it, so the
    extraction gold cannot silently disagree with the text it is supposed to
    describe. ``CandidateProfileDraft`` enforces the closed enum, so the only
    correct answer for ``本科`` is ``BACHELOR``.
    """
    canonical = education_canonical(surface)
    return canonical.upper() if canonical is not None else None


def _text_backing_for(archetype: _Archetype, rule_id: HardRuleId) -> SupportLevel:
    """§9.4: does the candidate's own text state the value this claim rests on?

    An UNKNOWN verdict has nothing to cite by definition, so it is INSUFFICIENT
    regardless of the text. Otherwise the label follows the *observed value*: a
    verbatim statement of it in the candidate's own text is what makes a claim
    traceable. See the module docstring on what this does and does not establish.
    """
    if archetype.outcomes[rule_id] is HardRuleOutcome.UNKNOWN:
        return SupportLevel.INSUFFICIENT
    return SupportLevel.SUPPORTED if rule_id in archetype.locatable else SupportLevel.PARTIAL


def _guarded(backing: SupportLevel, outcome: HardRuleOutcome) -> SupportLevel:
    """§4.5: a HIGH-impact claim that is not SUPPORTED is downgraded.

    ``FAIL`` is the only HIGH-impact hard-rule verdict, and the report rewrites such
    a claim with the "insufficient evidence" template and forces it to
    INSUFFICIENT. A labeller who reads §4.5 can predict that, which is why the
    corpus declares the post-guard level: declaring the raw text-backing level would
    make every unbacked FAIL look like a prediction error and drown out the errors
    that are real.
    """
    if outcome is HardRuleOutcome.FAIL and backing is not SupportLevel.SUPPORTED:
        return SupportLevel.INSUFFICIENT
    return backing


def _support_for(archetype: _Archetype, rule_id: HardRuleId) -> SupportLevel:
    """The level the report must show for one rule: §9.4, then §4.5."""
    return _guarded(_text_backing_for(archetype, rule_id), archetype.outcomes[rule_id])


def _overall_support(archetype: _Archetype) -> SupportLevel:
    """The same two steps, applied to the aggregate verdict.

    Mirrors ``reports.service._overall_support`` and then the guard: UNKNOWN overall
    -> INSUFFICIENT; every rule SUPPORTED -> SUPPORTED; otherwise the aggregate is
    only as strong as its weakest part. The aggregate consumes the *post-guard*
    per-rule levels, so a downgraded rule drags it down too.
    """
    overall = _overall_outcome(archetype)
    if overall is HardRuleOutcome.UNKNOWN:
        return SupportLevel.INSUFFICIENT
    backing = (
        SupportLevel.SUPPORTED
        if all(_support_for(archetype, rule) is SupportLevel.SUPPORTED for rule in HardRuleId)
        else SupportLevel.PARTIAL
    )
    return _guarded(backing, overall)


_RULE_NOTES: dict[HardRuleId, str] = {
    HardRuleId.YEARS_EXPERIENCE: "年限结论",
    HardRuleId.REQUIRED_EDUCATION: "学历结论",
    HardRuleId.REQUIRED_SKILLS: "技能结论",
}


def _conclusions(archetype: _Archetype) -> tuple[ConclusionGold, ...]:
    per_rule = tuple(
        ConclusionGold(
            claim_type=f"hard_rule:{rule.value}",
            expected_outcome=archetype.outcomes[rule],
            expected_support=_support_for(archetype, rule),
            note=_RULE_NOTES[rule],
        )
        for rule in (
            HardRuleId.YEARS_EXPERIENCE,
            HardRuleId.REQUIRED_EDUCATION,
            HardRuleId.REQUIRED_SKILLS,
        )
    )
    overall_outcome = _overall_outcome(archetype)
    return (
        *per_rule,
        ConclusionGold(
            claim_type="hard_rule_overall",
            expected_outcome=overall_outcome,
            expected_support=_overall_support(archetype),
            note="聚合结论",
        ),
    )


def _overall_outcome(archetype: _Archetype) -> HardRuleOutcome:
    outcomes = set(archetype.outcomes.values())
    if HardRuleOutcome.FAIL in outcomes:
        return HardRuleOutcome.FAIL
    if HardRuleOutcome.UNKNOWN in outcomes:
        return HardRuleOutcome.UNKNOWN
    return HardRuleOutcome.PASS


def _skills_for(archetype: _Archetype, family: JobFamily) -> tuple[str, ...]:
    resolved: list[str] = []
    for skill in archetype.skills:
        if skill == "__required__":
            resolved.extend(family.required_skills)
        elif skill == "__first_required__":
            resolved.append(family.required_skills[0])
        else:
            resolved.append(skill)
    return tuple(resolved)


CORPUS_NAME = "synthetic-raw-inputs"
CORPUS_VERSION = "v1"


def build_builtin_corpus() -> EvaluationCorpus:
    """Compose the corpus: every archetype against every job family.

    Deterministic by construction — no randomness, no clock, no environment — so
    the same commit always produces the same 52 cases and the same content hash.
    """
    cases: list[CorpusCase] = []
    for index, archetype in enumerate(_ARCHETYPES):
        for family_index, family in enumerate(JOB_FAMILIES):
            case_id = f"{archetype.key}@{family.key}"
            name = _CANDIDATE_NAMES[(index + family_index) % len(_CANDIDATE_NAMES)]
            sections = _render_sections(archetype, family, name=name)
            skills = _skills_for(archetype, family)
            cases.append(
                CorpusCase(
                    case_id=case_id,
                    split=archetype.split,
                    tags=frozenset(archetype.tags),
                    archetype=archetype.key,
                    job_family=family.key,
                    sections=sections,
                    job=family.to_query(),
                    extraction=ExtractionGold(
                        full_name=name,
                        skills=frozenset(skills),
                        education_level=_education_token(archetype.education_surface),
                    ),
                    confirmed=ConfirmedProfile(
                        normalized_skills=skills,
                        years_experience=archetype.years_experience,
                        education_level=archetype.education_level,
                    ),
                    relevant=archetype.relevant,
                    conclusions=_conclusions(archetype),
                    note=archetype.note,
                )
            )
    return EvaluationCorpus(
        name=CORPUS_NAME,
        version=CORPUS_VERSION,
        note=(
            "52 个由原始简历文本与岗位输入驱动的用例（13 类画像 × 4 类岗位）；"
            "标准答案在本模块中独立声明，不由预测器产出。"
        ),
        cases=tuple(cases),
    )


BUILTIN_CORPUS = build_builtin_corpus()

__all__ = [
    "BUILTIN_CORPUS",
    "CORPUS_NAME",
    "CORPUS_VERSION",
    "JOB_FAMILIES",
    "CaseTag",
    "ConclusionGold",
    "ConfirmedProfile",
    "CorpusCase",
    "EvaluationCorpus",
    "ExtractionGold",
    "JobFamily",
    "Split",
    "build_builtin_corpus",
]
