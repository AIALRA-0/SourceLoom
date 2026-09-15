"""Evidence-bearing responses for automatic production roles."""

from typing import Literal
from pydantic import Field
from .contracts import Strict, Evidence, Finding


class SourceFact(Strict):
    id: str
    source_id: str
    quote: str
    meaning: str = Field(min_length=1)
    person: str
    pronouns: list[str]
    referents: list[str]
    conditions: list[str]
    negations: list[str]
    quantities: list[str]
    modality: str
    note: str = ''


class FactInventory(Strict):
    facts: list[SourceFact] = Field(min_length=1)
    assessed_source_ids: list[str]
    unresolved: list[str]


class InventoryPatch(Strict):
    replacements: list[SourceFact]
    additions: list[SourceFact]
    remove_ids: list[str]
    unresolved: list[str]
    rationale: str = Field(min_length=1)


class InventoryUncertainty(Strict):
    id: str
    kind: Literal['original_ambiguity','extraction_gap','inventory_error']
    treatment: Literal['preserve_without_invention','requires_evidence','requires_correction']
    evidence: list[Evidence] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class InventoryAudit(Strict):
    assessed_source_ids: list[str]
    assessed_fact_ids: list[str]
    missing_facts: list[SourceFact]
    errors: list[str]
    unresolved: list[str]
    uncertainty_assessments: list[InventoryUncertainty]


class PlanReview(Strict):
    status: Literal['ready','needs_repair','needs_sources']
    issues: list[str]
    optional_source_limits: list[str]
    essential_missing_sources: list[str]


class CoverageAssignment(Strict):
    fact_id: str
    unit_id: str


class CoverageStageAddition(Strict):
    unit_id: str
    after_stage: str
    new_stage: str = Field(min_length=1)


class CoveragePatch(Strict):
    assignments: list[CoverageAssignment] = Field(min_length=1)
    stage_additions: list[CoverageStageAddition]
    rationale: str = Field(min_length=1)


class SourceReference(Strict):
    source_id: str


class PlanSelection(Strict):
    selected: Literal['original','repaired','none']
    status: Literal['ready','needs_repair','needs_sources']
    defects: list[str]
    rationale: str


class InventoryDecision(Strict):
    claim_index: int
    verdict: Literal['confirmed_error','not_error','unknown']
    evidence: list[SourceReference] = Field(min_length=1)
    reason: str = Field(min_length=1)


class InventoryDecisions(Strict):
    decisions: list[InventoryDecision]


class VisualPage(Strict):
    source_id: str
    source_markdown: str
    figure_descriptions: list[str]
    unresolved: list[str]


class VisualExtraction(Strict):
    pages: list[VisualPage]


class VisualPageAudit(Strict):
    source_id: str
    status: Literal['verified','needs_correction','unknown']
    discrepancies: list[str]
    region_evidence: list[str] = Field(min_length=1)


class VisualAudit(Strict):
    pages: list[VisualPageAudit]


class VisualCheck(Strict):
    category: Literal['text','reading_order','tables','formulas','figures','captions','footnotes']
    status: Literal['preserved','changed','missing','unreadable','not_applicable']
    region: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    transcript_excerpt: str = ''


class VisualPageChecks(Strict):
    source_id: str
    checks: list[VisualCheck]


class VisualAuditV2(Strict):
    pages: list[VisualPageChecks]


class FactCheck(Strict):
    fact_id: str
    source_quote: str
    block_id: str
    output_quote: str
    status: Literal['preserved','lost','changed','unknown']
    person_preserved: bool
    referents_preserved: bool
    explanation: str = Field(min_length=1)


class ReverseCheck(Strict):
    block_id: str
    output_quote: str = Field(min_length=1)
    kind: Literal['source','supplement']
    evidence: list[Evidence]
    status: Literal['supported','unsupported','unknown']
    explanation: str = Field(min_length=1)


class FidelityReview(Strict):
    assessed_source_ids: list[str]
    missing_inventory_information: list[str]
    fact_checks: list[FactCheck]
    reverse_checks: list[ReverseCheck]
    findings: list[Finding]


class RuleAssessment(Strict):
    rule_ids: list[str] = Field(min_length=1)
    status: Literal['pass','fail','unknown','not_applicable']
    block_ids: list[str]
    quotes: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class CandidateAssessment(Strict):
    candidate_id: str
    verdict: Literal['violation','not_violation','unknown']
    reason: str = Field(min_length=1)


class StyleReview(Strict):
    assessments: list[RuleAssessment]
    mechanical_assessments: list[CandidateAssessment]
    findings: list[Finding]


class IndependentReview(Strict):
    style: StyleReview
    fidelity: FidelityReview


class LocalEdit(Strict):
    block_id: str
    old_text: str = Field(min_length=1)
    new_text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class LocalRepair(Strict):
    document_digest: str
    edits: list[LocalEdit] = Field(min_length=1)


class LineEdit(Strict):
    line_id: str
    replacement: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class LineRepair(Strict):
    document_digest: str
    edits: list[LineEdit] = Field(min_length=1)


class PreparedTerm(Strict):
    zh: str = Field(min_length=1)
    en: str
    abbr: str
    source_ids: list[str] = Field(min_length=1)
    what: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    when: str = Field(min_length=1)
    boundary: str = Field(min_length=1)


class TermPreparation(Strict):
    terms: list[PreparedTerm]


class BindingAssignment(Strict):
    block_id: str
    obligation_ids: list[str]
    source_ids: list[str]


class BindingPatch(Strict):
    assignments: list[BindingAssignment] = Field(min_length=1)


class ReferenceTextEdit(Strict):
    block_id: str
    node_id: str
    old_text: str
    new_text: str


class ReferenceTextPatch(Strict):
    edits: list[ReferenceTextEdit] = Field(min_length=1)


class ParagraphNode(Strict):
    type: Literal['paragraph']
    text: str


class ListItem(Strict):
    text: str
    children: list['ListItem'] = []
    children_ordered: bool = False
    source_children: list['SourceNode'] = []


class ListNode(Strict):
    type: Literal['list']
    items: list[ListItem]
    ordered: bool = False


class TermNode(Strict):
    type: Literal['term']
    zh: str
    en: str
    abbr: str = ''
    definition: list[str] = Field(min_length=3,max_length=5)


class SourceNode(Strict):
    type: Literal['source']
    id: str
    presentation: Literal['raw','quote','code'] = 'raw'
    language: str | None = None
    layout: Literal['centered_image','centered_table'] | None = None
    caption: str | None = None


class CodeNode(Strict):
    type: Literal['code']
    language: str
    text: str


class FormulaNode(Strict):
    type: Literal['formula']
    text: str


class SectionNode(Strict):
    type: Literal['section']
    heading: str
    blocks: list['WritingNode']


WritingNode = ParagraphNode | ListNode | TermNode | SourceNode | CodeNode | FormulaNode | SectionNode
SectionNode.model_rebuild()


class ComposedBlock(Strict):
    id: str
    unit_id: str
    kind: Literal['source','explanation','example','exercise','extension','object','document_info']
    content: list[WritingNode] = Field(min_length=1)
    obligation_ids: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class ComposedDraft(Strict):
    blocks: list[ComposedBlock] = Field(min_length=1)


class NodeLocation(Strict):
    node_id: str
    parent_id: str


class FlatParagraph(NodeLocation, ParagraphNode):
    pass


class FlatTerm(NodeLocation, TermNode):
    pass


class FlatSource(NodeLocation, SourceNode):
    pass


class FlatCode(NodeLocation, CodeNode):
    pass


class FlatFormula(NodeLocation, FormulaNode):
    pass


class FlatSection(NodeLocation):
    type: Literal['section']
    heading: str


class FlatList(NodeLocation):
    type: Literal['list']
    ordered: bool


class FlatListItem(NodeLocation):
    type: Literal['list_item']
    text: str


FlatNode=FlatParagraph|FlatTerm|FlatSource|FlatCode|FlatFormula|FlatSection|FlatList|FlatListItem


class FlatBlock(ComposedBlock):
    content: list[FlatNode] = Field(min_length=1)


class FlatDraft(Strict):
    encoding: Literal['flat_nodes_v1']
    blocks: list[FlatBlock] = Field(min_length=1)


class UnitDependency(Strict):
    unit_id: str
    follows_units: list[str]
    bridge_reason: str


class DependencyPatch(Strict):
    units: list[UnitDependency]
