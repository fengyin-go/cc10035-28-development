"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.dom_parser import DomParserService
from app.services.entity_extract import EntityExtractService
from app.services.relation_graph import RelationGraphService
from app.services.content_classifier import ContentClassifierService
from app.services.template_rule import TemplateRuleService
from app.services.text_segment import TextSegmentService
from app.services.sentiment_tag import SentimentTagService
from app.services.schema_align import SchemaAlignService
from app.services.feed_scheduler import FeedSchedulerService
from app.services.quality_check import QualityCheckService
from app.services.probe_selector import ProbeSelectorService
from app.services.page_snapshot import PageSnapshotService
from app.services.extract_metric import ExtractMetricService
from app.services.nlu_result import NluResultService
from app.services.knowledge_link import KnowledgeLinkService
from app.services.output_target import OutputTargetService
from app.services.extract_studio import ExtractStudioService
from app.services.aggregate_view import AggregateViewService
from app.services.probe_delegation import ProbeDelegationService
from app.services.delegation_audit import DelegationAuditService

SERVICES: dict[str, BaseModuleService] = {
    "dom_parser": DomParserService(),
    "entity_extract": EntityExtractService(),
    "relation_graph": RelationGraphService(),
    "content_classifier": ContentClassifierService(),
    "template_rule": TemplateRuleService(),
    "text_segment": TextSegmentService(),
    "sentiment_tag": SentimentTagService(),
    "schema_align": SchemaAlignService(),
    "feed_scheduler": FeedSchedulerService(),
    "quality_check": QualityCheckService(),
    "probe_selector": ProbeSelectorService(),
    "page_snapshot": PageSnapshotService(),
    "extract_metric": ExtractMetricService(),
    "nlu_result": NluResultService(),
    "knowledge_link": KnowledgeLinkService(),
    "output_target": OutputTargetService(),
    "extract_studio": ExtractStudioService(),
    "aggregate_view": AggregateViewService(),
    "probe_delegation": ProbeDelegationService(),
    "delegation_audit": DelegationAuditService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
