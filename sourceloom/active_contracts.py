"""Source-bound planning artifacts; model output never controls job authority."""
from typing import Generic, Literal, TypeVar
from pydantic import Field
from .contracts import Strict


class RewriteContract(Strict):
    purpose: str = Field(min_length=1)
    reader_start: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    scope_boundary: str = Field(min_length=1)
    depth: Literal['faithful', 'clarify', 'progressive']
    permitted_additions: list[str]


class SourceObligation(Strict):
    id: str
    source_id: str
    quote: str = ''
    source_span_ids: list[str] = []
    meaning: str = Field(min_length=1)
    conditions: list[str]
    quantities: list[str]
    negations: list[str]
    narrator: str
    referents: list[str]


class NameEvidence(Strict):
    resource_id: str
    quote: str = Field(min_length=1)


class Abbreviation(Strict):
    short: str = Field(min_length=1)
    chinese: str = Field(min_length=1)
    english: str = Field(min_length=1)


class Concept(Strict):
    id: str
    name: str
    definition: str
    source_ids: list[str]
    requires: list[str]
    chinese_name: str = ''
    english_name: str = ''
    naming_status: Literal['unsearched','verified','ambiguous','unavailable','not_applicable'] = 'unsearched'
    name_evidence: list[NameEvidence] = []
    abbreviations: list[Abbreviation] = []
    naming_note: str = ''
    naming_status_reason: str = ''


class ExplanationPlan(Strict):
    known_start: str
    obstacle: str
    reasoning_steps: list[str]
    boundary: str


class CompositionNode(Strict):
    id: str
    title: str
    heading_level: int = Field(default=2, ge=2, le=5)
    purpose: str
    source_ids: list[str] = Field(min_length=1)
    obligation_ids: list[str] = Field(min_length=1)
    requires_concepts: list[str]
    establishes_concepts: list[str]
    depends_on: list[str]
    transition_from: str
    prepares_for: str
    depth: Literal['faithful', 'clarify', 'progressive']
    expansion: Literal['source_only', 'verified_clarification', 'authorized_example']
    explanation: ExplanationPlan
    section_outline: list[dict] = []
    cross_batch_risks: list[str] = Field(default_factory=list)


class LinkBrief(Strict):
    source_id: str
    role: Literal['content', 'navigation', 'administrative']
    topic: str = ''
    connection: str = ''
    destination: str = ''
    limitation: str = ''
    evidence: list[NameEvidence] = []
    unavailable_reason: str = ''


class EvidenceGap(Strict):
    """A bounded external lookup tied to one frozen source obligation."""
    id: str
    obligation_id: str
    source_id: str
    missing: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    allowed_sources: list[str] = Field(default_factory=list)
    stop_condition: str = Field(min_length=1)
    status: Literal['required', 'direct_read', 'query', 'candidate_selected',
                    'page_opened', 'evidence_bound', 'resolved', 'unresolved'] = 'required'


class EvidenceBinding(Strict):
    """Exact evidence bound to an EvidenceGap, never a search snippet."""
    id: str = ''
    gap_id: str
    obligation_id: str
    resource_id: str
    quote: str = Field(min_length=1)
    source_url: str = ''


class EvidenceResolution(Strict):
    """Auditable terminal or resumable state for one EvidenceGap."""
    gap_id: str
    status: Literal['resolved', 'unresolved', 'inaccessible']
    binding_ids: list[str] = Field(default_factory=list)
    attempts: int = Field(default=0, ge=0)
    stop_reason: str = Field(min_length=1)
    error: str = ''


class CompositionPlan(Strict):
    contract: RewriteContract
    obligations: list[SourceObligation] = Field(min_length=1)
    concepts: list[Concept]
    nodes: list[CompositionNode] = Field(min_length=1)
    link_briefs: list[LinkBrief] = []
    evidence_gaps: list[EvidenceGap] = []
    evidence_bindings: list[EvidenceBinding] = []
    evidence_resolutions: list[EvidenceResolution] = []


class CompositionPart(Strict):
    obligations: list[SourceObligation] = Field(min_length=1)
    concepts: list[Concept]
    nodes: list[CompositionNode] = Field(min_length=1)
    link_briefs: list[LinkBrief] = []
    evidence_gaps: list[EvidenceGap] = []
    evidence_bindings: list[EvidenceBinding] = []
    evidence_resolutions: list[EvidenceResolution] = []


class Action(Strict):
    kind: Literal['read', 'find', 'neighbors', 'search', 'page', 'image', 'release']
    resource_id: str = ''
    gap_id: str = ''
    source_id: str = ''
    search_profile: Literal['general', 'academic'] = 'general'
    query: str = ''
    start: int = Field(default=0, ge=0)
    end: int | None = Field(default=None, ge=0)
    url: str = ''


T = TypeVar('T')


class Turn(Strict, Generic[T]):
    gaps: list[str]
    ready_reason: str
    actions: list[Action]
    result: T | None


class Binding(Strict):
    obligation_id: str
    block_id: str
    output_quote: str = Field(min_length=1)


class KnowledgeDelta(Strict):
    established_concepts: list[str]
    explained_obligations: list[str]
    unresolved_prerequisites: list[str]
    next_bridge: str
    concept_evidence: list['ConceptBinding'] = []


class ConceptBinding(Strict):
    concept_id: str
    block_id: str
    output_quote: str = Field(min_length=1)


KnowledgeDelta.model_rebuild()


class AuthoredBlock(Strict):
    id: str
    kind: Literal['explanation','object','document_info']
    markdown: str = Field(min_length=1)
    obligation_ids: list[str]
    source_ids: list[str]


class WrittenUnit(Strict):
    blocks: list[AuthoredBlock] = Field(min_length=1)
    coverage: list[Binding] = []
    knowledge_delta: KnowledgeDelta


class VisualCard(Strict):
    source_id: str
    visible_content: str
    source_text: str
    role: Literal['text', 'data', 'diagram', 'formula', 'photo', 'decorative', 'mixed']
    relationships: list[str]
    uncertainty: list[str]
    limitations: list[str] = []
    blocking_uncertainty: list[str]


class VisualCards(Strict):
    cards: list[VisualCard]


class FormatEdit(Strict):
    block_id: str
    old_text: str = Field(min_length=1)
    new_text: str
    rule: str


class FormatLineEdit(Strict):
    line_id: str
    replacement: str
    reason: str = Field(min_length=1)


class FormatResolution(Strict):
    document_digest: str
    edits: list[FormatEdit]
    dismissed: list[str]
    reasons: list[str]
    line_edits: list[FormatLineEdit] = []
    requires_revision: list[str] = Field(default_factory=list, description='Exact finding/candidate IDs only, such as candidates-2. Put explanations in reasons, never append them to an ID.')


class ContentFinding(Strict):
    block_id: str
    output_quote: str = Field(min_length=1)
    source_id: str = ''
    source_quote: str = ''
    problem: str = Field(min_length=1)
    required_change: str = Field(min_length=1)


class FormatDecision(Strict):
    candidate_id: str
    decision: Literal['fix','dismiss']
    reason: str = Field(min_length=1)


class LinkAssessment(Strict):
    source_id: str
    block_id: str
    output_quote: str = Field(min_length=1)
    topic_explained: bool
    connection_explained: bool
    destination_explained: bool
    limitation_explained: bool
    image_explained: bool = True


class ContentReview(Strict):
    findings: list[ContentFinding]
    checked_obligation_ids: list[str]
    format_decisions: list[FormatDecision] = []
    link_assessments: list[LinkAssessment] = []
